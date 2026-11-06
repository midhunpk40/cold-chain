import json
import time
import uuid
from datetime import datetime, timezone

import boto3

REGION = "ap-south-1"
iot = boto3.client("iot", region_name=REGION)
EP = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
iot_data = boto3.client("iot-data", region_name=REGION, endpoint_url=f"https://{EP}")
lam = boto3.client("lambda", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
BUCKET = f"coldchain-raw-{boto3.client('sts').get_caller_identity()['Account']}"

START_S = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
THING = "coldchain-sensor-01"
state_table = ddb.Table("coldchain-sensor-state")
exc_table = ddb.Table("coldchain-excursions")
ship_table = ddb.Table("coldchain-shipments")

results = []
seq_box = {"n": None}


def next_seq():
    if seq_box["n"] is None:
        item = state_table.get_item(Key={"device_id": THING}, ConsistentRead=True).get("Item", {})
        seq_box["n"] = int(item.get("last_seq", 0))
    seq_box["n"] += 1
    return seq_box["n"]


def send_normal():
    msg = {
        "message_id": f"g-{uuid.uuid4().hex[:16]}",
        "seq": next_seq(),
        "device_id": "sensor-01",
        "shipment_id": "SHP-1001",
        "temperature_c": 5.0,
        "humidity_pct": 50.0,
        "battery_pct": 90,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    iot_data.publish(topic=f"coldchain/{THING}/telemetry", qos=1, payload=json.dumps(msg))
    time.sleep(1.5)


def watchdog(limit):
    r = lam.invoke(FunctionName="coldchain-watchdog", Payload=json.dumps({"max_silence_seconds": limit}).encode())
    body = json.loads(r["Payload"].read())
    if r.get("FunctionError"):
        raise SystemExit(f"watchdog error: {body}")
    return body


def actions(**payload):
    r = lam.invoke(FunctionName="coldchain-actions", Payload=json.dumps(payload).encode())
    return json.loads(r["Payload"].read())


def get_exc(exc_id):
    return exc_table.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item", {})


def my_gaps():
    items, kwargs = [], {}
    while True:  # small table: a scan is fine on a laptop
        page = exc_table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return [e for e in items if e.get("type") == "TELEMETRY_MISSING" and e["opened_at"] >= START_S]


def exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def wait_for(fn, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def check(name, ok):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}")


def finish():
    ok = sum(results)
    print(f"\n{ok}/{len(results)} passed")
    raise SystemExit(0 if ok == len(results) else 1)


st = state_table.get_item(Key={"device_id": THING}, ConsistentRead=True).get("Item", {})
if st.get("active_excursion_id") or st.get("active_gap_id"):
    raise SystemExit("sensor-01 has an active excursion or gap: close it first with 3 normal readings")

print("A. detection\n")
s1 = ship_table.get_item(Key={"shipment_id": "SHP-1001"}).get("Item", {})
s2 = ship_table.get_item(Key={"shipment_id": "SHP-1002"}).get("Item", {})
check("seed: SHP-1001 is monitored, SHP-1002 is not", s1.get("monitored") is True and s2.get("monitored") is False)

send_normal()
send_normal()
r = watchdog(60)
check("a device that just reported is not silent", r["watched"] == 1 and r["opened"] == [])

time.sleep(8)
r = watchdog(5)
check("a device silent for longer than the limit opens exactly one gap", len(r["opened"]) == 1)
if len(r["opened"]) != 1:
    finish()

gap_id = r["opened"][0]
e = get_exc(gap_id)
check("record type TELEMETRY_MISSING with violation telemetry_missing",
      e.get("type") == "TELEMETRY_MISSING" and e.get("violations") == ["telemetry_missing"])
check("condition ACTIVE and workflow OPEN",
      e.get("condition_state") == "ACTIVE" and e.get("workflow_status") == "OPEN")
check("limit snapshot stored (max_silence_seconds = 5)", int(e.get("limits", {}).get("max_silence_seconds", 0)) == 5)
check("opened.json evidence written", wait_for(lambda: exists(f"evidence/{gap_id}/opened.json"), 20))

r2 = watchdog(5)
check("a second run does not open a duplicate gap", r2["opened"] == [] and len(my_gaps()) == 1)
check("workflow picked it up (notified_at set, alert email sent)",
      wait_for(lambda: "notified_at" in get_exc(gap_id), 40))

print("\nB. recovery\n")
send_normal()
check("the next reading closes the gap (RECOVERED)",
      wait_for(lambda: get_exc(gap_id).get("condition_state") == "RECOVERED", 20))
check("gap lock cleared on the device",
      wait_for(lambda: "active_gap_id" not in state_table.get_item(
          Key={"device_id": THING}, ConsistentRead=True).get("Item", {}), 10))
check("closed.json written and gap_seconds recorded",
      wait_for(lambda: exists(f"evidence/{gap_id}/closed.json") and int(get_exc(gap_id).get("gap_seconds", 0)) >= 5, 20))

print("\nC. disposition rules\n")
LONG = "logger download checked, product stayed inside range"
r = actions(action="disposition", excursion_id=gap_id, actor="gaptest", decision="RELEASE", note=LONG)
check("release before acknowledgement is refused", r.get("error") == "not_acknowledged")
r = actions(action="acknowledge", excursion_id=gap_id, actor="gaptest", note="gap test")
check("acknowledge works", r.get("ok") is True)
r = actions(action="disposition", excursion_id=gap_id, actor="gaptest", decision="RELEASE", note="looks fine")
check("release of a gap without a written justification is refused", r.get("error") == "release_requires_justification")
r = actions(action="disposition", excursion_id=gap_id, actor="gaptest", decision="RELEASE", note=LONG)
check("release with a justification closes the incident", r.get("ok") is True and r.get("workflow_status") == "CLOSED")

finish()