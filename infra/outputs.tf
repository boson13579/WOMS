# Outputs of the WOMS stack.
#
# Populated as we add resources. Run `terraform output` after a successful
# apply to get the values the rest of the project needs (RDS endpoint, EKS
# cluster name, etc.).
#
# For sensitive outputs (passwords), mark them `sensitive = true` so they're
# masked when displayed but still readable via `terraform output -raw <name>`.

output "vpc_id" {
  description = "ID of the VPC."
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC."
  value       = module.vpc.vpc_cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (one per AZ)."
  value       = module.vpc.public_subnets
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (one per AZ). EKS nodes, RDS, and ElastiCache live here."
  value       = module.vpc.private_subnets
}

output "rds_endpoint" {
  description = "Hostname:port of the RDS PostgreSQL instance."
  value       = aws_db_instance.main.endpoint
}

output "rds_database_name" {
  description = "Initial database name on the RDS instance."
  value       = aws_db_instance.main.db_name
}

output "rds_username" {
  description = "Master username for the RDS instance."
  value       = aws_db_instance.main.username
}

output "rds_password" {
  description = "Master password (auto-generated). Mark sensitive so it's masked in normal terraform output."
  value       = random_password.db_password.result
  sensitive   = true
}

# Convenience: full DATABASE_URL string for the FastAPI backend.
# Read with: terraform output -raw database_url
output "database_url" {
  description = "Full PostgreSQL connection string for the backend."
  value       = "postgresql+psycopg://${aws_db_instance.main.username}:${random_password.db_password.result}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
  sensitive   = true
}

output "redis_endpoint" {
  description = "Hostname of the Redis primary endpoint."
  value       = aws_elasticache_cluster.main.cache_nodes[0].address
}

output "redis_port" {
  description = "Port the Redis cluster listens on."
  value       = aws_elasticache_cluster.main.port
}

# Convenience: full REDIS_URL string for the backend and Celery worker.
# Read with: terraform output -raw redis_url
output "redis_url" {
  description = "Full Redis connection string."
  value       = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:${aws_elasticache_cluster.main.port}/0"
}

output "eks_cluster_name" {
  description = "Name of the EKS cluster — pass to `aws eks update-kubeconfig` to get kubectl wired up."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "API server endpoint URL for the EKS cluster."
  value       = module.eks.cluster_endpoint
}

output "eks_oidc_provider_arn" {
  description = "OIDC provider ARN — needed in Phase 4 by the AWS Load Balancer Controller for IRSA."
  value       = module.eks.oidc_provider_arn
}

# Handy one-liner to print after apply so the user knows the next command.
output "kubeconfig_command" {
  description = "Run this to point your local kubectl at the cluster."
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}

output "alb_controller_role_arn" {
  description = "IAM role ARN for the AWS Load Balancer Controller. Passed to helm install via --set."
  value       = module.alb_controller_irsa_role.iam_role_arn
}

output "cloudfront_url" {
  description = "HTTPS URL of the CloudFront distribution (only set when alb_dns_name var is provided)."
  value       = local.cloudfront_enabled ? "https://${aws_cloudfront_distribution.main[0].domain_name}" : "(set var.alb_dns_name and re-apply to provision CloudFront)"
}

output "github_actions_role_arn" {
  description = "IAM role ARN to put in GitHub repo secrets as AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.github_actions.arn
}
