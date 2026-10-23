import datetime
import json
import os
import re
import uuid

import boto3

# create S3 client once, outside handler, so warm runs reuse it
s3 = boto3.client("s3")
BUCKET = os.environ["RAW_BUCKET"]


def handler(event, context):
    now = datetime.datetime.now(datetime.timezone.utc)

    # NEW: trust thing_name (from topic, set by IoT Rule) before device_id (from payload, can lie)
    raw_id = event.get("thing_name") or event.get("device_id", "unknown")
    # never trust input: keep only safe characters before using it in the S3 key
    device_id = re.sub(r"[^A-Za-z0-9_-]", "", str(raw_id))[:64] or "unknown"

    # random suffix stops two messages in the same second overwriting each other
    key = f"raw/{device_id}/{now:%Y/%m/%d}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.json"

    # store the original message untouched = audit evidence
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(event).encode("utf-8"),
        ContentType="application/json",
    )

    print(json.dumps({"stored": key}))
    return {"stored": key}