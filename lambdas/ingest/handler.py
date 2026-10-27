import datetime
import json
import math
import os
import re
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")

BUCKET = os.environ["RAW_BUCKET"]
readings = ddb.Table(os.environ["READINGS_TABLE"])
state = ddb.Table(os.environ["STATE_TABLE"])
shipments = ddb.Table(os.environ["SHIPMENTS_TABLE"])
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MSG_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_FUTURE = datetime.timedelta(minutes=5)  # device clock ahead = suspicious
MAX_AGE = datetime.timedelta(days=7)        # older than this = reject


def log(name, **fields):
    # one JSON line per event: easy to search, later feeds CloudWatch metrics
    print(json.dumps({"event": name, **fields}, default=str))


def iso(d):
    # fixed format so string comparison equals time comparison
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def safe(value):
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value))[:64] or "unknown"


def is_number(v):
    # bool is a subclass of int in Python, so exclude it explicitly
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_ts(value):
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def validate(event, now):
    errors = []
    thing = event.get("thing_name")  # added by IoT Rule from topic = trusted

    if not isinstance(thing, str) or not ID_RE.match(thing):
        errors.append("thing_name_missing_or_invalid")
    if not isinstance(event.get("message_id"), str) or not MSG_RE.match(event["message_id"]):
        errors.append("message_id_invalid")

    seq = event.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        errors.append("seq_invalid")

    ship = event.get("shipment_id")
    if not isinstance(ship, str) or not ID_RE.match(ship):
        errors.append("shipment_id_invalid")

    # physical plausibility, NOT product limits (those come in Step 7)
    for field, low, high in (("temperature_c", -60, 80), ("humidity_pct", 0, 100), ("battery_pct", 0, 100)):
        v = event.get(field)
        if not is_number(v):
            errors.append(f"{field}_missing_or_not_number")
        elif not low <= v <= high:
            errors.append(f"{field}_out_of_range")

    ts = None
    try:
        ts = parse_ts(event.get("timestamp"))
        if ts - now > MAX_FUTURE:
            errors.append("timestamp_in_future")
        elif now - ts > MAX_AGE:
            errors.append("timestamp_too_old")
    except (TypeError, ValueError):
        errors.append("timestamp_invalid")

    # payload claim must match the identity AWS verified from the topic
    claimed = event.get("device_id")
    if claimed is not None and isinstance(thing, str) and thing != f"coldchain-{claimed}":
        errors.append("device_id_mismatch")

    return ts, errors


def put_json(key, body):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(body, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def reject(raw_key, who, reasons, event):
    # quarantine: keep the bad message and why it failed, never lose evidence
    put_json(raw_key.replace("raw/", "rejected/", 1), {"reasons": reasons, "raw_key": raw_key, "event": event})
    log("rejected", thing=who, reasons=reasons, raw_key=raw_key)
    return {"status": "rejected", "reasons": reasons}


def touch(thing, seen):
    # device is alive even if this reading is old: update last_seen only
    state.update_item(
        Key={"device_id": thing},
        UpdateExpression="SET last_seen = :seen",
        ExpressionAttributeValues={":seen": seen},
    )


def handler(event, context):
    now = datetime.datetime.now(datetime.timezone.utc)
    now_s = iso(now)

    who = safe(event.get("thing_name") or event.get("device_id", "unknown"))
    raw_key = f"raw/{who}/{now:%Y/%m/%d}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.json"
    put_json(raw_key, event)  # 1. always keep the original, untouched

    ts, errors = validate(event, now)  # 2. validate
    if errors:
        return reject(raw_key, who, errors, event)

    thing = event["thing_name"]
    ship_id = event["shipment_id"]

    ship = shipments.get_item(Key={"shipment_id": ship_id}).get("Item")  # 3. known shipment, right device?
    if ship is None:
        return reject(raw_key, thing, ["unknown_shipment"], event)
    if ship.get("device_thing") != thing:
        return reject(raw_key, thing, ["device_not_assigned_to_shipment"], event)

    ts_s = iso(ts)
    st = state.get_item(Key={"device_id": thing}, ConsistentRead=True).get("Item")
    late = bool(st and "last_ts" in st and ts_s < st["last_ts"])  # older than newest known = late

    item = {
        "shipment_id": ship_id,
        "ts_id": f"{ts_s}#{event['message_id']}",
        "device_id": thing,
        "message_id": event["message_id"],
        "seq": event["seq"],
        "temperature_c": Decimal(str(event["temperature_c"])),
        "humidity_pct": Decimal(str(event["humidity_pct"])),
        "battery_pct": Decimal(str(event["battery_pct"])),
        "device_ts": ts_s,
        "received_at": now_s,
        "raw_key": raw_key,
        "late": late,
        "expires_at": int((now + datetime.timedelta(days=RETENTION_DAYS)).timestamp()),
    }

    try:
        # 4. dedupe: write only if this exact key does not exist yet
        readings.put_item(Item=item, ConditionExpression="attribute_not_exists(ts_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log("duplicate", thing=thing, message_id=event["message_id"])
            return {"status": "duplicate"}
        raise  # real error: let Lambda retry, the conditional write makes retry safe

    if late:
        touch(thing, now_s)
        log("stored_late", thing=thing, shipment=ship_id, seq=event["seq"], device_ts=ts_s)
        return {"status": "stored_late"}

    try:
        # 5. advance device state only if this reading is newer (guards against races too)
        old = state.update_item(
            Key={"device_id": thing},
            UpdateExpression="SET last_ts = :ts, last_seq = :seq, last_seen = :seen, shipment_id = :ship, "
                             "last_temp = :t, last_hum = :h, last_battery = :b",
            ConditionExpression="attribute_not_exists(last_ts) OR last_ts < :ts",
            ExpressionAttributeValues={
                ":ts": ts_s,
                ":seq": event["seq"],
                ":seen": now_s,
                ":ship": ship_id,
                ":t": item["temperature_c"],
                ":h": item["humidity_pct"],
                ":b": item["battery_pct"],
            },
            ReturnValues="ALL_OLD",
        ).get("Attributes")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            touch(thing, now_s)
            log("stored_late", thing=thing, shipment=ship_id, seq=event["seq"], device_ts=ts_s)
            return {"status": "stored_late"}
        raise

    if old and "last_seq" in old:
        last = int(old["last_seq"])
        if event["seq"] > last + 1:
            log("seq_gap", thing=thing, expected=last + 1, got=event["seq"])
        elif event["seq"] <= last:
            log("seq_reset", thing=thing, last=last, got=event["seq"])  # simulator restarted its counter

    log("stored", thing=thing, shipment=ship_id, seq=event["seq"],
        temperature_c=event["temperature_c"], humidity_pct=event["humidity_pct"])
    return {"status": "stored"}