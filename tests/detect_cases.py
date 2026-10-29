import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3

REGION = "ap-south-1"
iot = boto3.client("iot", region_name=REGION)
EP = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
iot_data = boto3.client("iot-data", region_name=REGION, endpoint_url=f"https://{EP}")
ddb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
BUCKET = f"coldchain-raw-{boto3.client('sts').get_caller_identity()['Account']}"

START = datetime.now(timezone.utc)
START_S = START.strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # same format the Lambda writes
GAP = 1.5     # seconds between messages: keeps arrival order deterministic
SETTLE = 6    # seconds to let the pipeline finish before checking

state_table = ddb.Table("coldchain-sensor-state")
exc_table = ddb.Table("coldchain-excursions")

S1, SH1 = "sensor-01", "SHP-1001"   # vaccine: 2-8 C, humidity 35-65, 3 bad readings open
S2, SH2 = "sensor-02", "SHP-1002"   # insulin: 2-8 C, humidity 30-70, 2 bad readings open
GOOD_T, GOOD_H = 5.0, 50.0

results = []
seqs = {}


def next_seq(thing):
    # continue from the device's last known seq, so the test adds no gap noise
    if thing not in seqs:
        item = state_table.get_item(Key={"device_id": thing}, ConsistentRead=True).get("Item", {})
        seqs[thing] = int(item.get("last_seq", 0))
    seqs[thing] += 1
    return seqs[thing]


def send(sensor, shipment, temp, hum, minutes_ago=0):
    thing = f"coldchain-{sensor}"
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    msg = {
        "message_id": f"d-{uuid.uuid4().hex[:16]}",
        "seq": next_seq(thing),
        "device_id": sensor,
        "shipment_id": shipment,
        "temperature_c": temp,
        "humidity_pct": hum,
        "battery_pct": 90,
        "timestamp": ts.isoformat(),
    }
    iot_data.publish(topic=f"coldchain/{thing}/telemetry", qos=1, payload=json.dumps(msg))
    time.sleep(GAP)


def settle():
    time.sleep(SETTLE)


def run_excursions(shipment):
    # excursions opened during this test run (small table, a scan is fine on a laptop)
    items, kwargs = [], {}
    while True:
        page = exc_table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    mine = [e for e in items if e["shipment_id"] == shipment and e["opened_at"] >= START_S]
    return sorted(mine, key=lambda e: e["opened_at"])


def get_exc(exc_id):
    return exc_table.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True)["Item"]


def evidence_exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def check(name, ok):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}")


def summary():
    ok = sum(1 for _, good in results if good)
    print(f"\n{ok}/{len(results)} passed")
    raise SystemExit(0 if ok == len(results) else 1)


# pre-flight: a device with an open excursion would disturb the test
for dev in ("coldchain-sensor-01", "coldchain-sensor-02"):
    st = state_table.get_item(Key={"device_id": dev}, ConsistentRead=True).get("Item", {})
    if st.get("active_excursion_id"):
        raise SystemExit(f"{dev} still has an active excursion ({st['active_excursion_id']}): "
                         f"send 3 normal readings with the simulator first")

print(f"test run started {START_S}\n")

# ---------------- vaccine (sensor-01) ----------------
print("A. vaccine shipment SHP-1001")

for t, h in ((8.0, GOOD_H), (2.0, GOOD_H), (GOOD_T, 65.0), (GOOD_T, 35.0)):
    send(S1, SH1, t, h)
settle()
check("limit values 8.0 / 2.0 C and 65 / 35 % are inside the range: no excursion",
      len(run_excursions(SH1)) == 0)

for t in (8.5, 9.0, GOOD_T):
    send(S1, SH1, t, GOOD_H)
settle()
check("glitch: 2 bad readings then a good one opens nothing", len(run_excursions(SH1)) == 0)

for _ in range(3):
    send(S1, SH1, 1.5, GOOD_H)
settle()
found = run_excursions(SH1)
check("3 consecutive cold readings open exactly 1 excursion", len(found) == 1)
if len(found) != 1:
    summary()

exc_id = found[0]["excursion_id"]
e = get_exc(exc_id)
check("violation recorded as temperature_low", e["violations"] == ["temperature_low"])
check("condition ACTIVE and workflow OPEN", e["condition_state"] == "ACTIVE" and e["workflow_status"] == "OPEN")
check("limits snapshot stored in the record", e["limits"]["temp_max"] == 8 and e["limits"]["hum_max"] == 65)
check("opened.json evidence written", evidence_exists(f"evidence/{exc_id}/opened.json"))

for _ in range(3):
    send(S1, SH1, 1.5, GOOD_H)
settle()
check("more bad readings while active do not open a second excursion", len(run_excursions(SH1)) == 1)

for t in (GOOD_T, GOOD_T, 9.2, GOOD_T, GOOD_T):   # 2 good, 1 bad, 2 good
    send(S1, SH1, t, GOOD_H)
settle()
check("relapse: 2 good, 1 bad, 2 good readings leave it ACTIVE",
      get_exc(exc_id)["condition_state"] == "ACTIVE")

send(S1, SH1, GOOD_T, GOOD_H)                    # third good reading in a row
settle()
e = get_exc(exc_id)
check("3 consecutive good readings recover it", e["condition_state"] == "RECOVERED")
check("workflow stays OPEN after recovery (a human decides)", e["workflow_status"] == "OPEN")
check("closed.json evidence written", evidence_exists(f"evidence/{exc_id}/closed.json"))
check("window stats correct: min 1.5, max 9.2, 12 readings",
      float(e.get("min_temp", -99)) == 1.5 and float(e.get("max_temp", -99)) == 9.2
      and int(e.get("reading_count", 0)) == 12)

for _ in range(3):
    send(S1, SH1, 9.5, GOOD_H, minutes_ago=5)    # hot, but older than the newest known reading
settle()
check("3 hot readings that arrive late open nothing", len(run_excursions(SH1)) == 1)

# ---------------- insulin (sensor-02) ----------------
print("\nB. insulin shipment SHP-1002 (different limits and sustain count)")

for _ in range(3):
    send(S2, SH2, GOOD_T, 68.0)
settle()
check("humidity 68 % is fine for insulin (limit 70, vaccine limit is 65)", len(run_excursions(SH2)) == 0)

for _ in range(2):
    send(S2, SH2, GOOD_T, 80.0)
settle()
found2 = run_excursions(SH2)
check("insulin opens after only 2 bad readings (vaccine needs 3)", len(found2) == 1)
if len(found2) == 1:
    check("violation recorded as humidity_high", get_exc(found2[0]["excursion_id"])["violations"] == ["humidity_high"])
    for _ in range(3):
        send(S2, SH2, GOOD_T, GOOD_H)
    settle()
    check("3 good readings recover it", get_exc(found2[0]["excursion_id"])["condition_state"] == "RECOVERED")
else:
    check("violation recorded as humidity_high", False)
    check("3 good readings recover it", False)

summary()