
data "archive_file" "actions" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/actions"
  output_path = "${path.module}/build/actions.zip"
}

resource "aws_iam_role" "actions" {
  name               = "coldchain-actions-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "actions" {
  name              = "/aws/lambda/coldchain-actions"
  retention_in_days = 7
}

data "aws_iam_policy_document" "actions" {
  # read the record, change its status; no create, no delete
  statement {
    sid       = "ExcursionRecord"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.excursions.arn]
  }

  # final record goes into the incident's evidence folder
  statement {
    sid       = "WriteDispositionEvidence"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/evidence/*"]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.actions.arn}:*"]
  }
}

resource "aws_iam_role_policy" "actions" {
  name   = "coldchain-actions-policy"
  role   = aws_iam_role.actions.id
  policy = data.aws_iam_policy_document.actions.json
}

resource "aws_lambda_function" "actions" {
  function_name    = "coldchain-actions"
  role             = aws_iam_role.actions.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.actions.output_path
  source_code_hash = data.archive_file.actions.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      EXCURSIONS_TABLE = aws_dynamodb_table.excursions.name
      RAW_BUCKET       = aws_s3_bucket.raw.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.actions, aws_iam_role_policy.actions]
}
