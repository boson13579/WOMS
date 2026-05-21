# Input variables for the WOMS infrastructure stack.
#
# Defaults are set so a fresh `terraform apply` would work without a
# tfvars file, but in practice values live in `terraform.tfvars` (gitignored).

variable "aws_region" {
  description = "AWS region for all resources. Locked to ap-northeast-1 for this project."
  type        = string
  default     = "ap-northeast-1"
}

variable "project" {
  description = "Short project name. Used as a prefix for resource names (e.g. woms-vpc, woms-eks)."
  type        = string
  default     = "woms"
}

variable "environment" {
  description = "Environment slug (dev / staging / prod). Tagged on every resource."
  type        = string
  default     = "dev"
}

variable "db_username" {
  description = "Master username for the RDS PostgreSQL instance."
  type        = string
  default     = "woms_admin"
}

variable "db_name" {
  description = "Initial database name created inside the RDS instance. Matches docker-compose POSTGRES_DB."
  type        = string
  default     = "smart_order"
}
