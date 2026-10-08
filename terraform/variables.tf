variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "alert_email" {
  type        = string
  description = "Email for budget and excursion alerts"
}

variable "budget_limit_usd" {
  type    = string
  default = "10"
}