import argparse
import json
import sys

import boto3

REGION = "ap-south-1"
lam = boto3.client("lambda", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table("coldchain-excursions")


def all_items():
    items, kwargs = [], {}
    while True:  # tiny table: a scan is fine on a laptop
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def resolve(exc_id):
    if exc_id != "latest":
        return exc_id
    items = all_items()
    if not items:
        sys.exit("no excursions yet")
    return max(items, key=lambda e: e["opened_at"])["excursion_id"]


def call(payload):
    res = lam.invoke(FunctionName="coldchain-actions", Payload=json.dumps(payload).encode("utf-8"))
    body = json.loads(res["Payload"].read())
    if res.get("FunctionError"):
        sys.exit(f"LAMBDA ERROR: {body}")
    print(json.dumps(body, indent=2))


p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)

a = sub.add_parser("ack")
a.add_argument("excursion_id")
a.add_argument("--actor", required=True)
a.add_argument("--note", default="")

d = sub.add_parser("dispose")
d.add_argument("excursion_id")
d.add_argument("decision")
d.add_argument("--actor", required=True)
d.add_argument("--note", default="")

s = sub.add_parser("show")
s.add_argument("excursion_id")

args = p.parse_args()
exc_id = resolve(args.excursion_id)

if args.cmd == "ack":
    call({"action": "acknowledge", "excursion_id": exc_id, "actor": args.actor, "note": args.note})
elif args.cmd == "dispose":
    call({"action": "disposition", "excursion_id": exc_id, "decision": args.decision.upper(),
          "actor": args.actor, "note": args.note})
else:
    item = table.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item")
    if not item:
        sys.exit("not found")
    keys = ("excursion_id", "shipment_id", "condition_state", "workflow_status", "acknowledged_by",
            "acknowledged_at", "disposition", "disposition_by", "disposition_at", "notified_at")
    print(json.dumps({k: item.get(k) for k in keys}, indent=2, default=str))