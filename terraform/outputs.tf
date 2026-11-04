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

output "escalations_topic_arn" {
  value = aws_sns_topic.escalations.arn
}

output "actions_function_name" {
  value = aws_lambda_function.actions.function_name
}

output "api_url" {
  value = aws_apigatewayv2_api.main.api_endpoint
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.main.id
}
