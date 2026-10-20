import datetime
import json
import os
import re
import uuid

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["RAW_BUCKET"]


def handler(event, context):
    now = datetime.datetime.now(datetime.timezone.utc)

    # never trust input: keep only safe characters for the S3 key
    device_id = re.sub(r"[^A-Za-z0-9_-]", "", str(event.get("device_id", "unknown")))[:64] or "unknown"

    key = f"raw/{device_id}/{now:%Y/%m/%d}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.json"

    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(event).encode("utf-8"),
        ContentType="application/json",
    )

    print(json.dumps({"stored": key}))
    return {"stored": key}