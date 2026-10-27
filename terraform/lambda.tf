data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingest" {
  name               = "coldchain-ingest-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/coldchain-ingest"
  retention_in_days = 7
}

data "aws_iam_policy_document" "ingest" {
  # raw copy + quarantine of bad messages
  statement {
    sid       = "WriteRawAndRejected"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/raw/*", "${aws_s3_bucket.raw.arn}/rejected/*"]
  }

  # read-only: check shipment exists and belongs to this device
  statement {
    sid       = "ReadShipments"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.shipments.arn]
  }

  # write-only: store readings (conditional PutItem = dedupe)
  statement {
    sid       = "WriteReadings"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.readings.arn]
  }

  # read + update device state, nothing else
  statement {
    sid       = "SensorState"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.sensor_state.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ingest.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ingest" {
  name   = "coldchain-ingest-policy"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/ingest"
  output_path = "${path.module}/build/ingest.zip"
}

resource "aws_lambda_function" "ingest" {
  function_name    = "coldchain-ingest"
  role             = aws_iam_role.ingest.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  timeout          = 10
  memory_size      = 256 # more memory = more CPU = faster start with boto3

  environment {
    variables = {
      RAW_BUCKET      = aws_s3_bucket.raw.id
      READINGS_TABLE  = aws_dynamodb_table.readings.name
      STATE_TABLE     = aws_dynamodb_table.sensor_state.name
      SHIPMENTS_TABLE = aws_dynamodb_table.shipments.name
      RETENTION_DAYS  = "30"
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy.ingest]
}
