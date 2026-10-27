import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

REGION = "ap-south-1"
iot = boto3.client("iot", region_name=REGION)
EP = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
iot_data = boto3.client("iot-data", region_name=REGION, endpoint_url=f"https://{EP}")
ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)

BUCKET = f"coldchain-raw-{boto3.client('sts').get_caller_identity()['Account']}"
LOG_GROUP = "/aws/lambda/coldchain-ingest"
RUN = uuid.uuid4().hex[:6]            # unique tag for this test run
START = datetime.now(timezone.utc)
MISSING = object()                    # marker: remove this field from the message
S1, S2 = "coldchain-sensor-01", "coldchain-sensor-02"

# continue sequence numbers from the device's last known value
st = ddb.Table("coldchain-sensor-state").get_item(Key={"device_id": S1}, ConsistentRead=True).get("Item", {})
seq = int(st.get("last_seq", 0))


def next_seq(step=1):
    global seq
    seq += step
    return seq


def now_iso(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat()


def build(case, **over):
    m = {
        "case": f"{RUN}:{case}",      # lets the checker find this message later
        "message_id": f"t-{RUN}-{uuid.uuid4().hex[:12]}",
        "seq": next_seq(),
        "device_id": "sensor-01",
        "shipment_id": "SHP-1001",
        "temperature_c": 5.0,
        "humidity_pct": 50.0,
        "battery_pct": 90,
        "timestamp": now_iso(),
    }
    for k, v in over.items():
        if v is MISSING:
            m.pop(k, None)
        else:
            m[k] = v
    return m


def send(thing, m):
    iot_data.publish(topic=f"coldchain/{thing}/telemetry", qos=1, payload=json.dumps(m))
    time.sleep(1.5)  # keep order deterministic: Lambda runs messages in parallel


cases = []  # (name, message, expectation)


def case(name, thing, m, expect):
    send(thing, m)
    cases.append((name, m, expect))


print(f"run {RUN}: sending test messages ...")

# --- good and tricky-but-valid cases ---
case("valid reading stored", S1, build("valid"), ("stored",))

dup = build("duplicate")
send(S1, dup)                                   # first delivery
case("duplicate delivery ignored", S1, dup, ("duplicate",))  # same message_id again

case("newer reading stored", S1, build("ooo_new"), ("stored",))
old = build("ooo_old", timestamp=now_iso(timedelta(minutes=-2)))
case("older reading arrives late: kept, flagged", S1, old, ("late",))

gap = build("gap")
gap["seq"] = next_seq(10)                       # skip 10 numbers = lost messages
case("sequence gap logged", S1, gap, ("gap", gap["seq"]))

# --- invalid cases: each must be quarantined with the right reason ---
naive_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()  # no timezone
bad = [
    ("missing temperature", S1, build("no_temp", temperature_c=MISSING), "temperature_c_missing_or_not_number"),
    ("temperature as text", S1, build("temp_text", temperature_c="5.2"), "temperature_c_missing_or_not_number"),
    ("temperature 999 (impossible)", S1, build("temp_range", temperature_c=999), "temperature_c_out_of_range"),
    ("humidity 150 (impossible)", S1, build("hum_range", humidity_pct=150), "humidity_pct_out_of_range"),
    ("timestamp 1 hour in future", S1, build("future", timestamp=now_iso(timedelta(hours=1))), "timestamp_in_future"),
    ("timestamp 10 days old", S1, build("too_old", timestamp=now_iso(timedelta(days=-10))), "timestamp_too_old"),
    ("timestamp without timezone", S1, build("no_tz", timestamp=naive_ts), "timestamp_invalid"),
    ("bad message_id", S1, build("bad_id", message_id="bad id!"), "message_id_invalid"),
    ("missing shipment_id", S1, build("no_ship", shipment_id=MISSING), "shipment_id_invalid"),
    ("unknown shipment", S1, build("unknown_ship", shipment_id="SHP-9999"), "unknown_shipment"),
    ("sensor-02 writes to sensor-01's shipment", S2, build("wrong_ship", device_id="sensor-02"), "device_not_assigned_to_shipment"),
    ("payload claims other device", S1, build("id_mismatch", device_id="sensor-02"), "device_id_mismatch"),
]
for name, thing, m, reason in bad:
    case(name, thing, m, ("rejected", reason))

print("waiting for the pipeline ...")
time.sleep(10)

# --- collect results ---
readings = {
    i["message_id"]: i
    for i in ddb.Table("coldchain-readings").query(
        KeyConditionExpression=Key("shipment_id").eq("SHP-1001"), ScanIndexForward=False, Limit=200
    )["Items"]
}

rejected = {}
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="rejected/"):
    for o in page.get("Contents", []):
        if o["LastModified"] >= START:
            body = json.loads(s3.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read())
            tag = body.get("event", {}).get("case")
            if tag:
                rejected[tag] = body["reasons"]


def log_has(pattern, needle):
    for _ in range(4):  # logs can lag a few seconds
        r = logs.filter_log_events(
            logGroupName=LOG_GROUP, startTime=int(START.timestamp() * 1000), filterPattern=pattern
        )
        if any(needle in e["message"] for e in r["events"]):
            return True
        time.sleep(5)
    return False


def passed(m, expect):
    mid, kind = m["message_id"], expect[0]
    if kind == "stored":
        return mid in readings and readings[mid]["late"] is False
    if kind == "duplicate":
        return mid in readings and log_has('"duplicate"', mid)
    if kind == "late":
        return mid in readings and readings[mid]["late"] is True
    if kind == "gap":
        return log_has('"seq_gap"', f'"got": {expect[1]}}}')
    if kind == "rejected":
        return expect[1] in rejected.get(m["case"], []) and mid not in readings
    return False


print()
ok = 0
for name, m, expect in cases:
    good = passed(m, expect)
    ok += good
    print(f"{'PASS' if good else 'FAIL'}  {name}")
print(f"\n{ok}/{len(cases)} passed")