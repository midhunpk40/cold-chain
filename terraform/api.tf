# ---------- API Lambda ----------
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/api"
  output_path = "${path.module}/build/api.zip"
}

resource "aws_iam_role" "api" {
  name               = "coldchain-api-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/coldchain-api"
  retention_in_days = 7
}

data "aws_iam_policy_document" "api" {
  # excursions: read by key and by the two indexes; no write (actions Lambda does that)
  statement {
    sid       = "ExcursionsRead"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.excursions.arn, "${aws_dynamodb_table.excursions.arn}/index/*"]
  }

  # shipments: tiny reference table, bounded scan allowed
  statement {
    sid       = "ShipmentsRead"
    actions   = ["dynamodb:GetItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.shipments.arn]
  }

  statement {
    sid       = "DeviceStateRead"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.sensor_state.arn]
  }

  statement {
    sid       = "ReadingsRead"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.readings.arn]
  }

  # approve/close go through the actions Lambda so business rules live in one place
  statement {
    sid       = "CallActions"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.actions.arn]
  }

  # evidence: list one prefix, read from it, nothing else in the bucket
  statement {
    sid       = "ListEvidence"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.raw.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["evidence/*"]
    }
  }

  statement {
    sid       = "ReadEvidence"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.raw.arn}/evidence/*"]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "coldchain-api-policy"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name    = "coldchain-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 15
  memory_size      = 256

  environment {
    variables = {
      EXCURSIONS_TABLE = aws_dynamodb_table.excursions.name
      SHIPMENTS_TABLE  = aws_dynamodb_table.shipments.name
      STATE_TABLE      = aws_dynamodb_table.sensor_state.name
      READINGS_TABLE   = aws_dynamodb_table.readings.name
      RAW_BUCKET       = aws_s3_bucket.raw.id
      ACTIONS_FUNCTION = aws_lambda_function.actions.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

# ---------- API Gateway (HTTP API) ----------
resource "aws_apigatewayv2_api" "main" {
  name          = "coldchain-api"
  protocol_type = "HTTP"
}

# gateway verifies signature, expiry, issuer and audience before Lambda runs
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.main.id
  authorizer_type  = "JWT"
  name             = "cognito-jwt"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.main.id]
    issuer   = "https://${aws_cognito_user_pool.main.endpoint}"
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

locals {
  api_routes = toset([
    "GET /excursions",
    "GET /excursions/{id}",
    "POST /excursions/{id}/ack",
    "POST /excursions/{id}/disposition",
    "GET /excursions/{id}/evidence",
    "GET /shipments",
    "GET /shipments/{id}",
  ])
}

resource "aws_apigatewayv2_route" "routes" {
  for_each           = local.api_routes
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

# every request that reaches the API, including 401 and 403 rejections
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/coldchain-api"
  retention_in_days = 7
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  # abuse and cost guard
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      time             = "$context.requestTime"
      ip               = "$context.identity.sourceIp"
      method           = "$context.httpMethod"
      route            = "$context.routeKey"
      path             = "$context.path"
      status           = "$context.status"
      authorizerError  = "$context.authorizer.error"
      authorizerClaims = "$context.authorizer.claims.username"
      responseLength   = "$context.responseLength"
    })
  }
}

# only this API may call the Lambda
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowApiGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
