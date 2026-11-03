output "raw_bucket_name" {
  value = aws_s3_bucket.raw.id
}

output "iot_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.excursion.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
