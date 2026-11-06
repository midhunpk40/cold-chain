# ---------- platform health: separate from medicine-excursion alerts ----------
resource "aws_sns_topic" "ops_alarms" {
  name = "coldchain-ops-alarms"
}

# stays PendingConfirmation until you click the link in the email
resource "aws_sns_topic_subscription" "ops_alarms_email" {
  topic_arn = aws_sns_topic.ops_alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# one alarm per Lambda: any error in 5 minutes
locals {
  alarmed_lambdas = {
    ingest   = aws_lambda_function.ingest.function_name
    actions  = aws_lambda_function.actions.function_name
    api      = aws_lambda_function.api.function_name
    watchdog = aws_lambda_function.watchdog.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.alarmed_lambdas
  alarm_name          = "coldchain-${each.key}-errors"
  alarm_description   = "Lambda ${each.value} reported errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

# workflow executions that ended FAILED
resource "aws_cloudwatch_metric_alarm" "workflow_failed" {
  alarm_name          = "coldchain-workflow-failed"
  alarm_description   = "Excursion workflow had a failed execution"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.excursion.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}

# API Gateway server-side errors
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "coldchain-api-5xx"
  alarm_description   = "API returned server errors"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.main.id }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
}
