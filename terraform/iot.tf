locals {
  sensors = toset(["sensor-01", "sensor-02"])
}

resource "aws_iot_thing" "sensor" {
  for_each = local.sensors
  name     = "coldchain-${each.key}"
}

resource "aws_iot_certificate" "sensor" {
  for_each = local.sensors
  active   = true
}

resource "aws_iot_policy" "sensor" {
  name = "coldchain-sensor-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "iot:Connect"
        Resource = "arn:aws:iot:${var.region}:${local.account_id}:client/$${iot:Connection.Thing.ThingName}"
      },
      {
        Effect   = "Allow"
        Action   = "iot:Publish"
        Resource = "arn:aws:iot:${var.region}:${local.account_id}:topic/coldchain/$${iot:Connection.Thing.ThingName}/telemetry"
      }
    ]
  })
}

resource "aws_iot_policy_attachment" "sensor" {
  for_each = local.sensors
  policy   = aws_iot_policy.sensor.name
  target   = aws_iot_certificate.sensor[each.key].arn
}

resource "aws_iot_thing_principal_attachment" "sensor" {
  for_each  = local.sensors
  thing     = aws_iot_thing.sensor[each.key].name
  principal = aws_iot_certificate.sensor[each.key].arn
}

resource "local_sensitive_file" "cert" {
  for_each = local.sensors
  filename = "${path.module}/../simulator/certs/${each.key}.cert.pem"
  content  = aws_iot_certificate.sensor[each.key].certificate_pem
}

resource "local_sensitive_file" "private_key" {
  for_each = local.sensors
  filename = "${path.module}/../simulator/certs/${each.key}.private.key"
  content  = aws_iot_certificate.sensor[each.key].private_key
}

data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

resource "aws_iot_topic_rule" "telemetry" {
  name    = "coldchain_telemetry" # underscores only, no hyphens
  enabled = true

  # + matches any sensor; topic(2) = thing name taken from the topic, which IoT policy enforces
  sql         = "SELECT *, topic(2) AS thing_name FROM 'coldchain/+/telemetry'"
  sql_version = "2016-03-23"

  # action: send every matching message to the ingest Lambda
  lambda {
    function_arn = aws_lambda_function.ingest.arn
  }
}

# Lambda door lock: allows IoT to call the function
# different from the Lambda role, which controls what Lambda may do
resource "aws_lambda_permission" "iot_invoke" {
  statement_id  = "AllowIoTRuleInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "iot.amazonaws.com"

  # only this one rule may call the function (stops confused deputy)
  source_arn     = aws_iot_topic_rule.telemetry.arn
  source_account = local.account_id
}
