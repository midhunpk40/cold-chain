import argparse
import hashlib
import json
import sys
from pathlib import Path

import boto3

REGION = "ap-south-1"
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table("coldchain-excursions")
s3 = boto3.client("s3", region_name=REGION)
BUCKET = f"coldchain-raw-{boto3.client('sts').get_caller_identity()['Account']}"


def resolve(exc_id):
    if exc_id != "latest":
        return exc_id
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not items:
        sys.exit("no excursions yet")
    return max(items, key=lambda e: e["opened_at"])["excursion_id"]


p = argparse.ArgumentParser()
p.add_argument("excursion_id", nargs="?", default="latest")
args = p.parse_args()
exc_id = resolve(args.excursion_id)

e = table.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item")
if not e:
    sys.exit("not found")

kind = e.get("type", "TEMPERATURE")  # old records have no type
lines = []


def add(text=""):
    lines.append(text)


add(f"# Excursion report: {exc_id}")
add()
add(f"- Type: {kind}")
add(f"- Shipment: {e.get('shipment_id')} | Device: {e.get('device_id')} | Product: {e.get('product_id')}")
add(f"- Violations: {', '.join(e.get('violations', []))}")
add(f"- Limits: {json.dumps(e.get('limits', {}), default=str)}")
add(f"- Condition: {e.get('condition_state')} | Workflow: {e.get('workflow_status')}")
add()
add("## Timeline (UTC)")
add()
events = [
    ("Started", e.get("started_at"), ""),
    ("Opened by system", e.get("opened_at"), ""),
    ("Team notified", e.get("notified_at"), ""),
    ("Acknowledged", e.get("acknowledged_at"), f"by {e.get('acknowledged_by')}"),
    ("Recovered", e.get("recovered_at"), ""),
    ("Disposition", e.get("disposition_at"),
     f"{e.get('disposition')} by {e.get('disposition_by')}, note: {e.get('disposition_note')}"),
]
for name, ts, extra in sorted((x for x in events if x[1]), key=lambda x: x[1]):
    add(f"- {ts}  {name} {extra}".rstrip())

add()
add("## Measurements")
add()
if kind == "TELEMETRY_MISSING":
    add(f"- Silence before detection: {e.get('silence_seconds')} s")
    add(f"- Total gap: {e.get('gap_seconds')} s (temperature unknown for the whole gap)")
else:
    add(f"- Readings in excursion: {e.get('reading_count')}")
    add(f"- Min / max temperature: {e.get('min_temp')} / {e.get('max_temp')} C")

add()
add("## Evidence files in S3 (SHA-256 computed now)")
add()
prefix = e.get("evidence_prefix", f"evidence/{exc_id}/")
objs = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
if not objs:
    add("- none found")
for o in objs:
    body = s3.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
    add(f"- {o['Key']} | {o['Size']} bytes | modified {o['LastModified']:%Y-%m-%dT%H:%M:%SZ} | "
        f"sha256 {hashlib.sha256(body).hexdigest()[:16]}...")

text = "\n".join(lines)
print(text)
out = Path("docs") / "reports"
out.mkdir(parents=True, exist_ok=True)
(out / f"{exc_id}.md").write_text(text, encoding="utf-8")
print(f"\nsaved docs\\reports\\{exc_id}.md")