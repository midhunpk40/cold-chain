# custom bus: keeps project events separate from AWS-internal ones
resource "aws_cloudwatch_event_bus" "main" {
  name = "coldchain-events"
}

# temporary debug target: shows every excursion event so routing can be seen
resource "aws_cloudwatch_log_group" "events" {
  name              = "/aws/events/coldchain"
  retention_in_days = 7
}

# lets EventBridge write into that log group
data "aws_iam_policy_document" "events_to_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.events.arn}:*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "events" {
  policy_name     = "coldchain-events-to-logs"
  policy_document = data.aws_iam_policy_document.events_to_logs.json
}

# the filter: only excursion events from the detector
resource "aws_cloudwatch_event_rule" "excursion" {
  name           = "coldchain-excursion-events"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = ["coldchain.detector"]
    "detail-type" = ["ExcursionOpened", "ExcursionRecovered"]
  })
}

resource "aws_cloudwatch_event_target" "excursion_logs" {
  rule           = aws_cloudwatch_event_rule.excursion.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  target_id      = "log-all"
  arn            = aws_cloudwatch_log_group.events.arn

  depends_on = [aws_cloudwatch_log_resource_policy.events]
}
