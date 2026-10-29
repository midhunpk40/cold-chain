import datetime
import json
import math
import os
import re
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
ddb = boto3.resource("dynamodb")

BUCKET = os.environ["RAW_BUCKET"]
readings = ddb.Table(os.environ["READINGS_TABLE"])
state = ddb.Table(os.environ["STATE_TABLE"])
shipments = ddb.Table(os.environ["SHIPMENTS_TABLE"])
products = ddb.Table(os.environ["PRODUCTS_TABLE"])
excursions = ddb.Table(os.environ["EXCURSIONS_TABLE"])
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MSG_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_FUTURE = datetime.timedelta(minutes=5)  # device clock ahead = suspicious
MAX_AGE = datetime.timedelta(days=7)        # older than this = reject


def jsonable(o):
    # DynamoDB numbers arrive as Decimal: write them as normal JSON numbers
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    return str(o)


def log(name, **fields):
    # one JSON line per event: easy to search, later feeds CloudWatch metrics
    print(json.dumps({"event": name, **fields}, default=jsonable))


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

    # physical plausibility, NOT product limits (those are checked after storing)
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
        Body=json.dumps(body, default=jsonable).encode("utf-8"),
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


# ---------------- excursion detection ----------------

def violations(temp, hum, lim):
    # limit values themselves are inside the range
    found = []
    if temp > lim["temp_max"]:
        found.append("temperature_high")
    if temp < lim["temp_min"]:
        found.append("temperature_low")
    if hum > lim["hum_max"]:
        found.append("humidity_high")
    if hum < lim["hum_min"]:
        found.append("humidity_low")
    return found


def txn(items):
    # all-or-nothing write across tables
    try:
        ddb.meta.client.transact_write_items(TransactItems=items)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            log("transaction_not_applied", reasons=reasons)
            return False
        raise


def readings_between(ship_id, start, end):
    # ts_id = "<timestamp>#<message_id>", so "#~" sorts after every id at the end timestamp
    kwargs = {
        "KeyConditionExpression": Key("shipment_id").eq(ship_id) & Key("ts_id").between(start, end + "#~"),
        "ConsistentRead": True,
    }
    items = []
    for _ in range(10):  # page cap keeps runtime bounded
        page = readings.query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def open_excursion(thing, ship_id, product_id, lim, new_bad, now_s):
    # the newest in-order readings are the ones that formed the bad streak
    recent = readings.query(
        KeyConditionExpression=Key("shipment_id").eq(ship_id),
        ScanIndexForward=False,
        Limit=50,
        ConsistentRead=True,
    )["Items"]
    streak = [r for r in recent if not r.get("late")][:new_bad]
    if not streak:
        return None

    kinds = sorted({k for r in streak for k in violations(r["temperature_c"], r["humidity_pct"], lim)})
    exc_id = f"EXC-{datetime.datetime.now(datetime.timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8]}"
    item = {
        "excursion_id": exc_id,
        "shipment_id": ship_id,
        "device_id": thing,
        "product_id": product_id,
        "violations": kinds or ["unknown"],
        "condition_state": "ACTIVE",    # what the temperature is doing
        "workflow_status": "OPEN",      # what humans did about it
        "started_at": streak[-1]["device_ts"],
        "opened_at": now_s,
        "trigger_count": len(streak),
        "limits": lim,                  # snapshot: limits may change later
        "evidence_prefix": f"evidence/{exc_id}/",
    }

    applied = txn([
        {   # claim the single "active excursion" slot of this device
            "Update": {
                "TableName": state.name,
                "Key": {"device_id": thing},
                "UpdateExpression": "SET active_excursion_id = :id",
                "ConditionExpression": "attribute_not_exists(active_excursion_id)",
                "ExpressionAttributeValues": {":id": exc_id},
            }
        },
        {   # create the record in the same transaction
            "Put": {
                "TableName": excursions.name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(excursion_id)",
            }
        },
    ])
    if not applied:
        return None

    try:
        put_json(item["evidence_prefix"] + "opened.json", {"excursion": item, "trigger_readings": streak})
    except Exception as e:  # excursion is already recorded, do not fail the message
        log("evidence_failed", excursion_id=exc_id, error=str(e))

    log("excursion_opened", excursion_id=exc_id, shipment=ship_id, thing=thing, violations=kinds)
    return exc_id


def close_excursion(thing, exc_id, ts_s, now_s):
    exc = excursions.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item")
    if not exc:
        log("excursion_record_missing", excursion_id=exc_id)
        return None

    applied = txn([
        {   # free the slot, only if it still holds this excursion
            "Update": {
                "TableName": state.name,
                "Key": {"device_id": thing},
                "UpdateExpression": "REMOVE active_excursion_id",
                "ConditionExpression": "active_excursion_id = :id",
                "ExpressionAttributeValues": {":id": exc_id},
            }
        },
        {   # temperature recovered; workflow_status stays OPEN until a human decides
            "Update": {
                "TableName": excursions.name,
                "Key": {"excursion_id": exc_id},
                "UpdateExpression": "SET condition_state = :rec, recovered_at = :ts, recovery_logged_at = :now",
                "ConditionExpression": "condition_state = :act",
                "ExpressionAttributeValues": {":rec": "RECOVERED", ":act": "ACTIVE", ":ts": ts_s, ":now": now_s},
            }
        },
    ])
    if not applied:
        return None

    try:
        rows = readings_between(exc["shipment_id"], exc["started_at"], ts_s)
        temps = [r["temperature_c"] for r in rows]
        stats = {"reading_count": len(rows), "min_temp": min(temps), "max_temp": max(temps)}
        excursions.update_item(
            Key={"excursion_id": exc_id},
            UpdateExpression="SET reading_count = :n, min_temp = :lo, max_temp = :hi",
            ExpressionAttributeValues={":n": stats["reading_count"], ":lo": stats["min_temp"], ":hi": stats["max_temp"]},
        )
        put_json(exc["evidence_prefix"] + "closed.json", {
            "excursion_id": exc_id,
            "started_at": exc["started_at"],
            "recovered_at": ts_s,
            "stats": stats,
            "readings": rows[:500],
        })
    except Exception as e:
        log("evidence_failed", excursion_id=exc_id, error=str(e))

    log("excursion_recovered", excursion_id=exc_id, thing=thing)
    return exc_id


# ---------------- handler ----------------

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

    product = products.get_item(Key={"product_id": ship["product_id"]}).get("Item")
    if product is None:
        return reject(raw_key, thing, ["unknown_product"], event)
    lim = {k: product[k] for k in ("temp_min", "temp_max", "hum_min", "hum_max")}
    sustain = int(product["sustain_count"])
    recover = int(product["recover_count"])

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
        # late readings are history: they never change streaks or device state
        touch(thing, now_s)
        log("stored_late", thing=thing, shipment=ship_id, seq=event["seq"], device_ts=ts_s)
        return {"status": "stored_late"}

    found = violations(item["temperature_c"], item["humidity_pct"], lim)
    bad = bool(found)
    if bad:
        streak_expr = "bad_streak = if_not_exists(bad_streak, :zero) + :one, good_streak = :zero"
    else:
        streak_expr = "good_streak = if_not_exists(good_streak, :zero) + :one, bad_streak = :zero"

    try:
        # 5. advance device state only if newer; streak counters change in the same atomic update
        old = state.update_item(
            Key={"device_id": thing},
            UpdateExpression="SET last_ts = :ts, last_seq = :seq, last_seen = :seen, shipment_id = :ship, "
                             "last_temp = :t, last_hum = :h, last_battery = :b, " + streak_expr,
            ConditionExpression="attribute_not_exists(last_ts) OR last_ts < :ts",
            ExpressionAttributeValues={
                ":ts": ts_s,
                ":seq": event["seq"],
                ":seen": now_s,
                ":ship": ship_id,
                ":t": item["temperature_c"],
                ":h": item["humidity_pct"],
                ":b": item["battery_pct"],
                ":zero": 0,
                ":one": 1,
            },
            ReturnValues="ALL_OLD",
        ).get("Attributes") or {}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            touch(thing, now_s)
            log("stored_late", thing=thing, shipment=ship_id, seq=event["seq"], device_ts=ts_s)
            return {"status": "stored_late"}
        raise

    if "last_seq" in old:
        last = int(old["last_seq"])
        if event["seq"] > last + 1:
            log("seq_gap", thing=thing, expected=last + 1, got=event["seq"])
        elif event["seq"] <= last:
            log("seq_reset", thing=thing, last=last, got=event["seq"])  # simulator restarted its counter

    # streak values after this reading (same arithmetic DynamoDB just did)
    new_bad = int(old.get("bad_streak", 0)) + 1 if bad else 0
    new_good = 0 if bad else int(old.get("good_streak", 0)) + 1
    active = old.get("active_excursion_id")

    outcome = None
    if bad and not active and new_bad >= sustain:
        outcome = {"opened": open_excursion(thing, ship_id, ship["product_id"], lim, new_bad, now_s)}
    elif not bad and active and new_good >= recover:
        outcome = {"recovered": close_excursion(thing, active, ts_s, now_s)}

    log("stored", thing=thing, shipment=ship_id, seq=event["seq"],
        temperature_c=event["temperature_c"], humidity_pct=event["humidity_pct"],
        violations=found, bad_streak=new_bad, good_streak=new_good)
    return {"status": "stored", "excursion": outcome}