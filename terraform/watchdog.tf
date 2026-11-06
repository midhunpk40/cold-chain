data "archive_file" "watchdog" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/watchdog"
  output_path = "${path.module}/build/watchdog.zip"
}

resource "aws_iam_role" "watchdog" {
  name               = "coldchain-watchdog-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/coldchain-watchdog"
  retention_in_days = 7
}

data "aws_iam_policy_document" "watchdog" {
  statement {
    sid       = "FindMonitoredShipments"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.shipments.arn]
  }

  statement {
    sid       = "DeviceState"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.sensor_state.arn]
  }

  statement {
    sid       = "CreateGapRecord"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.excursions.arn]
  }

  statement {
    sid       = "WriteEvidence"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/evidence/*"]
  }

  statement {
    sid       = "EmitEvents"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.main.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.watchdog.arn}:*"]
  }
}

resource "aws_iam_role_policy" "watchdog" {
  name   = "coldchain-watchdog-policy"
  role   = aws_iam_role.watchdog.id
  policy = data.aws_iam_policy_document.watchdog.json
}

resource "aws_lambda_function" "watchdog" {
  function_name    = "coldchain-watchdog"
  role             = aws_iam_role.watchdog.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.watchdog.output_path
  source_code_hash = data.archive_file.watchdog.output_base64sha256
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      SHIPMENTS_TABLE     = aws_dynamodb_table.shipments.name
      STATE_TABLE         = aws_dynamodb_table.sensor_state.name
      EXCURSIONS_TABLE    = aws_dynamodb_table.excursions.name
      RAW_BUCKET          = aws_s3_bucket.raw.id
      EVENT_BUS           = aws_cloudwatch_event_bus.main.name
      MAX_SILENCE_SECONDS = tostring(var.max_silence_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.watchdog, aws_iam_role_policy.watchdog]
}

# scheduled rules live on the default bus (no event_bus_name)
resource "aws_cloudwatch_event_rule" "watchdog" {
  name                = "coldchain-watchdog-schedule"
  schedule_expression = "rate(1 minute)"
  state               = var.watchdog_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "watchdog" {
  rule      = aws_cloudwatch_event_rule.watchdog.name
  target_id = "run-watchdog"
  arn       = aws_lambda_function.watchdog.arn
}

# only this rule may invoke the watchdog
resource "aws_lambda_permission" "watchdog_schedule" {
  statement_id  = "AllowScheduleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.watchdog.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watchdog.arn
}
