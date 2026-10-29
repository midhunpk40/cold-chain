import json

import boto3
from boto3.dynamodb.conditions import Key

ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
account = boto3.client("sts").get_caller_identity()["Account"]
bucket = f"coldchain-raw-{account}"

print("== readings ==")
for ship in ("SHP-1001", "SHP-1002"):
    items = ddb.Table("coldchain-readings").query(KeyConditionExpression=Key("shipment_id").eq(ship))["Items"]
    for i in items:
        print(f"{ship} {i['ts_id'][:26]} seq={i['seq']} temp={i['temperature_c']} late={i['late']}")

print("== sensor state ==")
for s in ddb.Table("coldchain-sensor-state").scan()["Items"]:
    print(s["device_id"], "last_ts=", s.get("last_ts"), "last_seq=", s.get("last_seq"), "last_seen=", s.get("last_seen"))

print("== rejected (latest 10) ==")
objs = s3.list_objects_v2(Bucket=bucket, Prefix="rejected/").get("Contents", [])
for o in sorted(objs, key=lambda x: x["LastModified"])[-10:]:
    body = json.loads(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read())
    print(o["Key"].split("/")[-1], body["reasons"])
print("== excursions ==")
for e in ddb.Table("coldchain-excursions").scan()["Items"]:
    print(e["excursion_id"], e["shipment_id"], e["violations"], e["condition_state"], e["workflow_status"],
          "started", e["started_at"][:19], "recovered", e.get("recovered_at", "-")[:19],
          "min/max", e.get("min_temp"), e.get("max_temp"))    