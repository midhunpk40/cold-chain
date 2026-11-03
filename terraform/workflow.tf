# ---------- SNS: where alert emails go ----------
resource "aws_sns_topic" "alerts" {
  name = "coldchain-alerts"
}

# email stays PendingConfirmation until the recipient clicks the link
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------- role the state machine runs as ----------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "coldchain-workflow-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  # send the alert email, to this one topic only
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  # claim the excursion so duplicate events do nothing
  statement {
    sid       = "ClaimNotification"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.excursions.arn]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "coldchain-workflow-policy"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

# ---------- the workflow ----------
resource "aws_sfn_state_machine" "excursion" {
  name     = "coldchain-excursion-workflow"
  role_arn = aws_iam_role.sfn.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Excursion opened: claim once, then notify the team"
    StartAt = "ClaimNotification"
    States = {

      # first copy of the event wins; a repeat fails the condition
      ClaimNotification = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:updateItem"
        Parameters = {
          TableName           = aws_dynamodb_table.excursions.name
          Key                 = { excursion_id = { "S.$" = "$.detail.excursion_id" } }
          UpdateExpression    = "SET notified_at = :now"
          ConditionExpression = "attribute_not_exists(notified_at)"
          ExpressionAttributeValues = {
            ":now" = { "S.$" = "$$.State.EnteredTime" }
          }
        }
        ResultPath = null # keep the original event as the state input
        Catch = [{
          # both spellings listed: the service prefix casing differs between docs
          ErrorEquals = ["DynamoDB.ConditionalCheckFailedException", "DynamoDb.ConditionalCheckFailedException"]
          Next        = "AlreadyHandled"
        }]
        Next = "NotifyTeam"
      }

      NotifyTeam = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.alerts.arn
          "Subject.$" = "States.Format('[COLD-CHAIN] Excursion {} on {}', $.detail.excursion_id, $.detail.shipment_id)"
          "Message.$" = "States.Format('Temperature excursion detected. Excursion: {} | Shipment: {} | Device: {} | Product: {} | Violations: {} | Started at (UTC): {} | Limits: {} | Next step: investigate and record a disposition.', $.detail.excursion_id, $.detail.shipment_id, $.detail.device_id, $.detail.product_id, States.JsonToString($.detail.violations), $.detail.started_at, States.JsonToString($.detail.limits))"
        }
        ResultPath = null
        Retry = [{
          ErrorEquals     = ["States.TaskFailed"]
          IntervalSeconds = 2
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "NotifyFailed"
        }]
        End = true
      }

      AlreadyHandled = {
        Type = "Succeed"
      }

      NotifyFailed = {
        Type  = "Fail"
        Error = "NotificationFailed"
        Cause = "SNS publish failed after retries"
      }
    }
  })
}

# ---------- EventBridge starts the workflow ----------
data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_to_sfn" {
  name               = "coldchain-events-to-workflow-role"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
}

data "aws_iam_policy_document" "events_to_sfn" {
  statement {
    sid       = "StartWorkflow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.excursion.arn]
  }
}

resource "aws_iam_role_policy" "events_to_sfn" {
  name   = "coldchain-events-to-workflow-policy"
  role   = aws_iam_role.events_to_sfn.id
  policy = data.aws_iam_policy_document.events_to_sfn.json
}

# a second rule on the same bus: only "opened" events start the workflow
resource "aws_cloudwatch_event_rule" "excursion_opened" {
  name           = "coldchain-excursion-opened"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = ["coldchain.detector"]
    "detail-type" = ["ExcursionOpened"]
  })
}

resource "aws_cloudwatch_event_target" "excursion_workflow" {
  rule           = aws_cloudwatch_event_rule.excursion_opened.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  target_id      = "start-workflow"
  arn            = aws_sfn_state_machine.excursion.arn
  role_arn       = aws_iam_role.events_to_sfn.arn
}
