# WOMS infrastructure — root config.
#
# Provider + terraform settings. Resource definitions live in topic-specific
# files added as we go through Phase 2:
#   vpc.tf           — network (VPC, subnets, NAT, IGW, security groups)
#   rds.tf           — PostgreSQL on RDS
#   elasticache.tf   — Redis on ElastiCache
#   eks.tf           — EKS cluster + node group
#
# Usage:
#   terraform init       (first time, or after adding a new provider)
#   terraform plan       (preview changes — ALWAYS run before apply)
#   terraform apply      (creates / updates resources)
#   terraform destroy    (tears everything down — use between work sessions
#                         to save money)

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # Every resource Terraform creates gets these tags automatically.
  # Useful for cost allocation in AWS Cost Explorer (filter by `Project=woms`).
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
