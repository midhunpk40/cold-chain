import base64
import json
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

REGION = os.environ["AWS_REGION"]
ddb = boto3.resource("dynamodb")
lam = boto3.client("lambda")
s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))

excursions = ddb.Table(os.environ["EXCURSIONS_TABLE"])
shipments = ddb.Table(os.environ["SHIPMENTS_TABLE"])
sensor_state = ddb.Table(os.environ["STATE_TABLE"])
readings = ddb.Table(os.environ["READINGS_TABLE"])
BUCKET = os.environ["RAW_BUCKET"]
ACTIONS_FN = os.environ["ACTIONS_FUNCTION"]

EXC_RE = re.compile(r"^EXC-[0-9]{8}-[0-9a-f]{8}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATUSES = {"OPEN", "ACKNOWLEDGED", "CLOSED"}
ALL = {"Quality", "Operations", "Auditor"}

# who may call which route; a route missing here does not exist
ALLOWED = {
    "GET /excursions": ALL,
    "GET /excursions/{id}": ALL,
    "POST /excursions/{id}/ack": {"Quality", "Operations"},
    "POST /excursions/{id}/disposition": {"Quality"},
    "GET /excursions/{id}/evidence": {"Quality", "Auditor"},
    "GET /shipments": ALL,
    "GET /shipments/{id}": ALL,
}

# actions-Lambda error code -> HTTP status
HTTP_FOR = {
    "bad_excursion_id": 400, "bad_actor": 400, "bad_decision": 400, "unknown_action": 400,
    "not_found": 404,
    "not_open": 409, "not_acknowledged": 409, "release_requires_recovery": 409,
}

SUMMARY_KEYS = ("excursion_id", "shipment_id", "device_id", "product_id", "violations",
                "condition_state", "workflow_status", "started_at", "acknowledged_by", "disposition")


class ApiError(Exception):
    def __init__(self, code, error, message):
        self.code, self.error, self.message = code, error, message


def jsonable(o):
    # DynamoDB numbers arrive as Decimal: write them as normal JSON numbers
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def log(name, **fields):
    print(json.dumps({"event": name, **fields}, default=jsonable))


def respond(code, body):
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, default=jsonable),
    }


def parse_groups(raw):
    # API Gateway hands array claims over as the text "[Quality Operations]"
    if isinstance(raw, list):
        return set(raw)
    text = str(raw or "").strip().strip("[]")
    return {g for g in re.split(r"[,\s]+", text) if g}


def parse_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > 4096:
        raise ApiError(413, "body_too_large", "request body over 4 KB")
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        raise ApiError(400, "bad_json", "body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "body must be a JSON object")
    return obj


def summary(item):
    return {k: item.get(k) for k in SUMMARY_KEYS}


def need_id(path, pattern, what):
    value = (path or {}).get("id")
    if not isinstance(value, str) or not pattern.match(value):
        raise ApiError(400, "bad_id", f"{what} id missing or malformed")
    return value


# ---------------- excursions ----------------

def list_excursions(q):
    status = q.get("status")
    ship = q.get("shipment_id")
    if status and status.upper() not in STATUSES:
        raise ApiError(400, "bad_status", "status must be OPEN, ACKNOWLEDGED or CLOSED")

    if ship:
        if not ID_RE.match(ship):
            raise ApiError(400, "bad_shipment_id", "shipment_id malformed")
        items = excursions.query(
            IndexName="by_shipment", KeyConditionExpression=Key("shipment_id").eq(ship),
            ScanIndexForward=False, Limit=50)["Items"]
        if status:
            items = [i for i in items if i.get("workflow_status") == status.upper()]
    else:
        wanted = [status.upper()] if status else sorted(STATUSES)
        items = []
        for s in wanted:
            items += excursions.query(
                IndexName="by_status", KeyConditionExpression=Key("workflow_status").eq(s),
                ScanIndexForward=False, Limit=50)["Items"]

    items.sort(key=lambda e: e["started_at"], reverse=True)
    items = items[:50]
    return 200, {"count": len(items), "excursions": [summary(i) for i in items]}


def get_excursion(exc_id):
    item = excursions.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiError(404, "not_found", "no such excursion")
    return item


def run_action(action, exc_id, actor, body):
    payload = {"action": action, "excursion_id": exc_id, "actor": actor, "note": body.get("note", "")}
    if action == "disposition":
        payload["decision"] = str(body.get("decision", "")).upper()
    res = lam.invoke(FunctionName=ACTIONS_FN, Payload=json.dumps(payload).encode("utf-8"))
    out = json.loads(res["Payload"].read())
    if res.get("FunctionError"):
        log("actions_failed", excursion_id=exc_id, detail=str(out)[:300])
        raise ApiError(502, "actions_failed", "could not complete the action")
    if out.get("ok"):
        return 200, out
    return HTTP_FOR.get(out.get("error"), 400), out


def evidence(exc_id):
    get_excursion(exc_id)  # 404 if it does not exist
    prefix = f"evidence/{exc_id}/"
    objs = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
    files = [{
        "name": o["Key"].split("/")[-1],
        "size": o["Size"],
        "url": s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": o["Key"]}, ExpiresIn=300),
    } for o in objs]
    return 200, {"excursion_id": exc_id, "expires_in_seconds": 300, "files": files}


# ---------------- shipments ----------------

def list_shipments():
    items = shipments.scan(Limit=50)["Items"]  # tiny reference table: a bounded scan is fine
    return 200, {"count": len(items), "shipments": items}


def get_shipment(sid):
    ship = shipments.get_item(Key={"shipment_id": sid}).get("Item")
    if not ship:
        raise ApiError(404, "not_found", "no such shipment")
    st = sensor_state.get_item(Key={"device_id": ship["device_thing"]}).get("Item") or {}
    rows = readings.query(
        KeyConditionExpression=Key("shipment_id").eq(sid), ScanIndexForward=False, Limit=10)["Items"]
    excs = excursions.query(
        IndexName="by_shipment", KeyConditionExpression=Key("shipment_id").eq(sid),
        ScanIndexForward=False, Limit=10)["Items"]
    keys = ("last_ts", "last_seen", "last_temp", "last_hum", "last_battery", "active_excursion_id")
    return 200, {
        "shipment": ship,
        "device_state": {k: st.get(k) for k in keys},
        "recent_readings": [{k: r.get(k) for k in ("device_ts", "temperature_c", "humidity_pct",
                                                  "battery_pct", "seq", "late")} for r in rows],
        "recent_excursions": [summary(e) for e in excs],
    }


# ---------------- routing ----------------

def route(rk, event, actor):
    path = event.get("pathParameters")
    q = event.get("queryStringParameters") or {}

    if rk == "GET /excursions":
        return list_excursions(q)
    if rk == "GET /shipments":
        return list_shipments()
    if rk == "GET /shipments/{id}":
        return get_shipment(need_id(path, ID_RE, "shipment"))

    exc_id = need_id(path, EXC_RE, "excursion")
    if rk == "GET /excursions/{id}":
        return 200, get_excursion(exc_id)
    if rk == "GET /excursions/{id}/evidence":
        return evidence(exc_id)
    if rk == "POST /excursions/{id}/ack":
        return run_action("acknowledge", exc_id, actor, parse_body(event))
    if rk == "POST /excursions/{id}/disposition":
        return run_action("disposition", exc_id, actor, parse_body(event))
    raise ApiError(404, "not_found", "unknown route")


def handler(event, context):
    rk = event.get("routeKey")
    claims = (event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}) or {}).get("claims", {}) or {}
    actor = claims.get("username") or claims.get("cognito:username")  # verified by the gateway
    groups = parse_groups(claims.get("cognito:groups"))

    allowed = ALLOWED.get(rk)
    if allowed is None:
        return respond(404, {"error": "not_found", "message": "unknown route"})
    if not actor or not (groups & allowed):
        log("forbidden", actor=actor, groups=sorted(groups), route=rk)
        return respond(403, {"error": "forbidden", "message": "your group may not do this"})

    try:
        code, body = route(rk, event, actor)
    except ApiError as e:
        code, body = e.code, {"error": e.error, "message": e.message}
    except Exception as e:  # never leak internals to the caller
        log("api_error", actor=actor, route=rk, error=str(e)[:300])
        code, body = 500, {"error": "internal_error", "message": "something went wrong"}

    log("request", actor=actor, groups=sorted(groups), route=rk, status=code)
    return respond(code, body)