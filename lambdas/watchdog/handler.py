import datetime
import json
import os
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
events = boto3.client("events")

shipments = ddb.Table(os.environ["SHIPMENTS_TABLE"])
state = ddb.Table(os.environ["STATE_TABLE"])
EXCURSIONS = os.environ["EXCURSIONS_TABLE"]
BUCKET = os.environ["RAW_BUCKET"]
EVENT_BUS = os.environ["EVENT_BUS"]
DEFAULT_LIMIT = int(os.environ["MAX_SILENCE_SECONDS"])


def jsonable(o):
    # DynamoDB numbers arrive as Decimal: write them as normal JSON numbers
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    return str(o)


def log(name, **fields):
    print(json.dumps({"event": name, **fields}, default=jsonable))


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def put_json(key, body):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(body, default=jsonable).encode("utf-8"),
        ContentType="application/json",
    )


def emit(detail_type, detail):
    # announce what happened; never fail the run if the bus is unavailable
    try:
        res = events.put_events(Entries=[{
            "EventBusName": EVENT_BUS,
            "Source": "coldchain.detector",
            "DetailType": detail_type,
            "Detail": json.dumps(detail, default=jsonable),
        }])
        if res.get("FailedEntryCount"):
            log("event_emit_failed", detail_type=detail_type, excursion_id=detail.get("excursion_id"),
                error=res["Entries"][0].get("ErrorMessage"))
        else:
            log("event_emitted", detail_type=detail_type, excursion_id=detail.get("excursion_id"))
    except Exception as e:
        log("event_emit_failed", detail_type=detail_type, excursion_id=detail.get("excursion_id"), error=str(e))


def monitored_shipments():
    # tiny reference table: a bounded scan is fine
    items, kwargs = [], {"FilterExpression": Attr("monitored").eq(True)}
    for _ in range(10):
        page = shipments.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def open_gap(ship, st, silence, limit, now, now_s):
    thing = ship["device_thing"]
    exc_id = f"EXC-{now:%Y%m%d}-{uuid.uuid4().hex[:8]}"
    item = {
        "excursion_id": exc_id,
        "type": "TELEMETRY_MISSING",
        "shipment_id": ship["shipment_id"],
        "device_id": thing,
        "product_id": ship["product_id"],
        "violations": ["telemetry_missing"],
        "condition_state": "ACTIVE",
        "workflow_status": "OPEN",
        "started_at": st["last_seen"],        # last time the device was heard from
        "opened_at": now_s,
        "silence_seconds": int(silence),
        "limits": {"max_silence_seconds": limit},
        "evidence_prefix": f"evidence/{exc_id}/",
    }

    try:
        ddb.meta.client.transact_write_items(TransactItems=[
            {   # claim the gap slot, but only if the device has not spoken since we looked
                "Update": {
                    "TableName": state.name,
                    "Key": {"device_id": thing},
                    "UpdateExpression": "SET active_gap_id = :id",
                    "ConditionExpression": "attribute_not_exists(active_gap_id) AND last_seen = :seen",
                    "ExpressionAttributeValues": {":id": exc_id, ":seen": st["last_seen"]},
                }
            },
            {   # create the record in the same transaction
                "Put": {
                    "TableName": EXCURSIONS,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(excursion_id)",
                }
            },
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            log("gap_not_opened", thing=thing, reasons=reasons)
            return None
        raise

    try:
        put_json(item["evidence_prefix"] + "opened.json", {
            "excursion": item,
            "last_known_state": {k: st.get(k) for k in
                                 ("last_ts", "last_seen", "last_seq", "last_temp", "last_hum", "last_battery")},
        })
    except Exception as e:  # the gap is already recorded: do not fail the run
        log("evidence_failed", excursion_id=exc_id, error=str(e))

    log("gap_opened", excursion_id=exc_id, shipment=item["shipment_id"], thing=thing, silence_seconds=int(silence))
    emit("ExcursionOpened", item)
    return exc_id


def handler(event, context):
    now = datetime.datetime.now(datetime.timezone.utc)
    now_s = iso(now)

    limit = DEFAULT_LIMIT
    override = event.get("max_silence_seconds") if isinstance(event, dict) else None
    if isinstance(override, int) and not isinstance(override, bool) and 5 <= override <= 86400:
        limit = override  # test hook: only callers allowed to invoke this function can use it

    watched = monitored_shipments()
    silent, opened = 0, []

    for ship in watched:
        thing = ship["device_thing"]
        st = state.get_item(Key={"device_id": thing}, ConsistentRead=True).get("Item")
        if not st or "last_seen" not in st:
            log("device_never_seen", thing=thing, shipment=ship["shipment_id"])
            continue
        if st.get("active_gap_id"):
            silent += 1  # already reported, wait for the device to speak again
            continue

        last_seen = datetime.datetime.fromisoformat(st["last_seen"])
        silence = (now - last_seen).total_seconds()
        if silence <= limit:
            continue

        silent += 1
        gap_id = open_gap(ship, st, silence, limit, now, now_s)
        if gap_id:
            opened.append(gap_id)

    log("watchdog_run", watched=len(watched), silent=silent, opened=len(opened), limit_seconds=limit)
    return {"watched": len(watched), "silent": silent, "opened": opened, "limit_seconds": limit}