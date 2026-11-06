import datetime
import json
import os
import re
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
excursions = ddb.Table(os.environ["EXCURSIONS_TABLE"])
BUCKET = os.environ["RAW_BUCKET"]

EXC_RE = re.compile(r"^EXC-[0-9]{8}-[0-9a-f]{8}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
DECISIONS = {"RELEASE", "QUARANTINE", "DESTROY"}


def jsonable(o):
    # DynamoDB numbers arrive as Decimal: write them as normal JSON numbers
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    return str(o)


def log(name, **fields):
    print(json.dumps({"event": name, **fields}, default=jsonable))


def now_s():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def clean_note(value):
    text = str(value or "").strip()[:500]
    return text or "-"


def fail(code, message, **extra):
    log("action_rejected", error=code, message=message, **extra)
    return {"ok": False, "error": code, "message": message, **extra}


def get_exc(exc_id):
    return excursions.get_item(Key={"excursion_id": exc_id}, ConsistentRead=True).get("Item")


def check_ids(event):
    # never trust input: shape-check before it touches the database
    exc_id = event.get("excursion_id")
    actor = event.get("actor")
    if not isinstance(exc_id, str) or not EXC_RE.match(exc_id):
        return None, None, fail("bad_excursion_id", "excursion_id missing or malformed")
    if not isinstance(actor, str) or not ACTOR_RE.match(actor):
        return None, None, fail("bad_actor", "actor missing or malformed")
    return exc_id, actor, None


def acknowledge(event):
    exc_id, actor, err = check_ids(event)
    if err:
        return err
    note = clean_note(event.get("note"))
    ts = now_s()

    try:
        excursions.update_item(
            Key={"excursion_id": exc_id},
            UpdateExpression="SET workflow_status = :ack, acknowledged_by = :actor, "
                             "acknowledged_at = :ts, ack_note = :note",
            ConditionExpression="workflow_status = :open",  # only OPEN can be acknowledged
            ExpressionAttributeValues={
                ":ack": "ACKNOWLEDGED", ":open": "OPEN", ":actor": actor, ":ts": ts, ":note": note,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        item = get_exc(exc_id)
        if item is None:
            return fail("not_found", "no such excursion", excursion_id=exc_id)
        return fail("not_open", "excursion is not OPEN", excursion_id=exc_id,
                    current_status=item.get("workflow_status"))

    log("acknowledged", excursion_id=exc_id, actor=actor)
    return {"ok": True, "excursion_id": exc_id, "workflow_status": "ACKNOWLEDGED",
            "acknowledged_by": actor, "acknowledged_at": ts}


def disposition(event):
    exc_id, actor, err = check_ids(event)
    if err:
        return err
    decision = event.get("decision")
    if decision not in DECISIONS:
        return fail("bad_decision", "decision must be RELEASE, QUARANTINE or DESTROY")
    note = clean_note(event.get("note"))
    ts = now_s()
    if decision == "RELEASE":
        current = get_exc(exc_id)
        if current and current.get("type") == "TELEMETRY_MISSING" and len(note) < 20:
            # temperature was unknown during the gap: release needs a written reason
            return fail("release_requires_justification",
                        "temperature was unknown during a telemetry gap: add a note of at least 20 characters "
                        "explaining why release is safe", excursion_id=exc_id)
                        

    condition = "workflow_status = :ack"  # only an acknowledged excursion can be closed
    values = {
        ":ack": "ACKNOWLEDGED", ":closed": "CLOSED", ":d": decision,
        ":actor": actor, ":ts": ts, ":note": note,
    }
    if decision == "RELEASE":
        # medicine may only be released once the temperature is back in range
        condition += " AND condition_state = :rec"
        values[":rec"] = "RECOVERED"

    try:
        res = excursions.update_item(
            Key={"excursion_id": exc_id},
            UpdateExpression="SET workflow_status = :closed, disposition = :d, disposition_by = :actor, "
                             "disposition_at = :ts, disposition_note = :note",
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        item = get_exc(exc_id)
        if item is None:
            return fail("not_found", "no such excursion", excursion_id=exc_id)
        if item.get("workflow_status") != "ACKNOWLEDGED":
            return fail("not_acknowledged", "excursion must be ACKNOWLEDGED first", excursion_id=exc_id,
                        current_status=item.get("workflow_status"))
        return fail("release_requires_recovery", "temperature has not recovered yet: choose QUARANTINE or DESTROY, "
                    "or wait for recovery", excursion_id=exc_id, condition_state=item.get("condition_state"))

    final = res["Attributes"]
    try:  # audit evidence: the final record, kept with the rest of the incident files
        s3.put_object(
            Bucket=BUCKET,
            Key=f"evidence/{exc_id}/disposition.json",
            Body=json.dumps(final, default=jsonable).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:  # record is already closed: do not undo it
        log("evidence_failed", excursion_id=exc_id, error=str(e))

    log("closed", excursion_id=exc_id, actor=actor, decision=decision)
    return {"ok": True, "excursion_id": exc_id, "workflow_status": "CLOSED",
            "disposition": decision, "disposition_by": actor, "disposition_at": ts}


def handler(event, context):
    action = event.get("action")
    if action == "acknowledge":
        return acknowledge(event)
    if action == "disposition":
        return disposition(event)
    return fail("unknown_action", "action must be acknowledge or disposition")