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
variable "poll_seconds" {
  type        = number
  default     = 15
  description = "how often the workflow checks the excursion record"
}

variable "ack_escalate_seconds" {
  type        = number
  default     = 90
  description = "no acknowledgement after this long: send the escalation email"
}

variable "ack_expire_seconds" {
  type        = number
  default     = 240
  description = "no acknowledgement after this long: execution fails (AckExpired)"
}

variable "disposition_expire_seconds" {
  type        = number
  default     = 300
  description = "acknowledged but no disposition after this long: execution fails"
}

variable "escalation_email" {
  type        = string
  default     = ""
  description = "manager email; empty means reuse alert_email"
}
variable "watchdog_enabled" {
  type        = bool
  default     = false
  description = "run the silent-sensor check every minute (keep false while the simulator is idle)"
}

variable "max_silence_seconds" {
  type        = number
  default     = 120
  description = "a monitored sensor silent for longer than this opens a telemetry gap"
}
