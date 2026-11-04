resource "aws_cognito_user_pool" "main" {
  name = "coldchain-users"

  # nobody signs up alone: admin creates every user
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  mfa_configuration   = "OFF"      # prototype; production turns MFA on
  deletion_protection = "INACTIVE" # lets terraform destroy remove it after demo
}

# CLI-only client for testing: password login, no secret
resource "aws_cognito_user_pool_client" "main" {
  name            = "coldchain-api-client"
  user_pool_id    = aws_cognito_user_pool.main.id
  generate_secret = false

  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED" # wrong user and wrong password look the same

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

locals {
  cognito_groups = {
    Quality    = "Quality and administration: decides final disposition"
    Operations = "Warehouse and operations: acknowledges excursions"
    Auditor    = "Read-only: views records and evidence"
  }
}

resource "aws_cognito_user_group" "group" {
  for_each     = local.cognito_groups
  name         = each.key
  user_pool_id = aws_cognito_user_pool.main.id
  description  = each.value
}
