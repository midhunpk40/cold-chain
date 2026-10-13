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
