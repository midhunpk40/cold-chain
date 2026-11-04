locals {
  # timers in seconds become loop counts; the check at count N happens about (N + 1) * poll_seconds in
  escalate_polls    = max(ceil(var.ack_escalate_seconds / var.poll_seconds) - 1, 0)
  ack_expire_polls  = max(ceil(var.ack_expire_seconds / var.poll_seconds) - 1, 0)
  disp_expire_polls = max(ceil(var.disposition_expire_seconds / var.poll_seconds) - 1, 0)
  escalation_target = var.escalation_email != "" ? var.escalation_email : var.alert_email
}

# ---------- SNS: where emails go ----------
resource "aws_sns_topic" "alerts" {
  name = "coldchain-alerts"
}

# email stays PendingConfirmation until the recipient clicks the link
resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# second audience: managers, for overdue incidents
resource "aws_sns_topic" "escalations" {
  name = "coldchain-escalations"
}

resource "aws_sns_topic_subscription" "escalation_email" {
  topic_arn = aws_sns_topic.escalations.arn
  protocol  = "email"
  endpoint  = local.escalation_target
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
  # send emails, to these two topics only
  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn, aws_sns_topic.escalations.arn]
  }

  # claim the excursion (UpdateItem) and watch its status (GetItem)
  statement {
    sid       = "ExcursionRecord"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
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
    Comment = "Excursion opened: notify, wait for acknowledgement (escalate if late), wait for disposition"
    StartAt = "ClaimNotification"
    States = {

      # ---- Step 9 part: claim once, then notify ----
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
        ResultPath = null
        Catch = [{
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
          "Message.$" = "States.Format('Temperature excursion detected. Excursion: {} | Shipment: {} | Device: {} | Product: {} | Violations: {} | Started at (UTC): {} | Limits: {} | Next step: acknowledge, investigate and record a disposition.', $.detail.excursion_id, $.detail.shipment_id, $.detail.device_id, $.detail.product_id, States.JsonToString($.detail.violations), $.detail.started_at, States.JsonToString($.detail.limits))"
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
        Next = "InitAckLoop"
      }

      # ---- phase A: wait for acknowledgement ----
      InitAckLoop = {
        Type = "Pass"
        Parameters = {
          "detail.$" = "$.detail"
          polls      = 0
        }
        Next = "WaitForAck"
      }

      WaitForAck = {
        Type    = "Wait"
        Seconds = var.poll_seconds
        Next    = "GetStatusAck"
      }

      GetStatusAck = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:getItem"
        Parameters = {
          TableName      = aws_dynamodb_table.excursions.name
          Key            = { excursion_id = { "S.$" = "$.detail.excursion_id" } }
          ConsistentRead = true
        }
        ResultPath = "$.excursion"
        Next       = "CheckAck"
      }

      CheckAck = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.excursion.Item.workflow_status.S"
            StringEquals = "ACKNOWLEDGED"
            Next         = "InitDispositionLoop"
          },
          {
            Variable     = "$.excursion.Item.workflow_status.S"
            StringEquals = "CLOSED"
            Next         = "Done"
          },
          {
            Variable                 = "$.polls"
            NumericGreaterThanEquals = local.ack_expire_polls
            Next                     = "AckExpired"
          },
          {
            Variable      = "$.polls"
            NumericEquals = local.escalate_polls
            Next          = "EscalateUnacknowledged"
          }
        ]
        Default = "IncrementAck"
      }

      EscalateUnacknowledged = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.escalations.arn
          "Subject.$" = "States.Format('[COLD-CHAIN][ESCALATION] Excursion {} not acknowledged', $.detail.excursion_id)"
          "Message.$" = "States.Format('Excursion {} on shipment {} is still unacknowledged. Device: {} | Product: {} | Violations: {} | Started at (UTC): {}. Immediate action needed.', $.detail.excursion_id, $.detail.shipment_id, $.detail.device_id, $.detail.product_id, States.JsonToString($.detail.violations), $.detail.started_at)"
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
        Next = "IncrementAck"
      }

      IncrementAck = {
        Type = "Pass"
        Parameters = {
          "detail.$" = "$.detail"
          "polls.$"  = "States.MathAdd($.polls, 1)"
        }
        Next = "WaitForAck"
      }

      AckExpired = {
        Type  = "Fail"
        Error = "AckExpired"
        Cause = "No acknowledgement before the deadline"
      }

      # ---- phase B: wait for disposition ----
      InitDispositionLoop = {
        Type = "Pass"
        Parameters = {
          "detail.$" = "$.detail"
          polls      = 0
        }
        Next = "WaitForDisposition"
      }

      WaitForDisposition = {
        Type    = "Wait"
        Seconds = var.poll_seconds
        Next    = "GetStatusDisposition"
      }

      GetStatusDisposition = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:getItem"
        Parameters = {
          TableName      = aws_dynamodb_table.excursions.name
          Key            = { excursion_id = { "S.$" = "$.detail.excursion_id" } }
          ConsistentRead = true
        }
        ResultPath = "$.excursion"
        Next       = "CheckDisposition"
      }

      CheckDisposition = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.excursion.Item.workflow_status.S"
            StringEquals = "CLOSED"
            Next         = "NotifyClosed"
          },
          {
            Variable                 = "$.polls"
            NumericGreaterThanEquals = local.disp_expire_polls
            Next                     = "DispositionExpired"
          }
        ]
        Default = "IncrementDisposition"
      }

      IncrementDisposition = {
        Type = "Pass"
        Parameters = {
          "detail.$" = "$.detail"
          "polls.$"  = "States.MathAdd($.polls, 1)"
        }
        Next = "WaitForDisposition"
      }

      NotifyClosed = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.alerts.arn
          "Subject.$" = "States.Format('[COLD-CHAIN] Excursion {} CLOSED: {}', $.detail.excursion_id, $.excursion.Item.disposition.S)"
          "Message.$" = "States.Format('Excursion {} on shipment {} is closed. Disposition: {} | Decided by: {} at (UTC): {} | Note: {} | Acknowledged by: {}.', $.detail.excursion_id, $.detail.shipment_id, $.excursion.Item.disposition.S, $.excursion.Item.disposition_by.S, $.excursion.Item.disposition_at.S, $.excursion.Item.disposition_note.S, $.excursion.Item.acknowledged_by.S)"
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
        Next = "Done"
      }

      DispositionExpired = {
        Type  = "Fail"
        Error = "DispositionExpired"
        Cause = "Acknowledged but no disposition before the deadline"
      }

      # ---- ends ----
      Done = {
        Type = "Succeed"
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
