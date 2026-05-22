# Network layer — VPC, public/private subnets across 2 AZs, NAT, IGW.
#
# We use the official terraform-aws-modules/vpc/aws module because hand-rolling
# a production VPC is ~150 lines of boilerplate (route tables, NAT EIPs,
# associations) that distract from understanding EKS itself. The module is
# the de-facto standard — ~50M+ downloads, well-documented, and its outputs
# plug directly into the EKS module we'll add later.
#
# Cost: VPC, subnets, IGW, route tables are FREE. The only cost is the
# **NAT Gateway** at $0.045/hr (~$1.08/day if left running). Apply at the
# start of a work session, destroy when done.

# Pick 2 AZs in the current region. Defaults work for ap-northeast-1 (1a, 1c).
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.project}-${var.environment}"
  cidr = "10.0.0.0/16"

  azs             = local.azs
  public_subnets  = ["10.0.0.0/24", "10.0.1.0/24"]
  private_subnets = ["10.0.10.0/24", "10.0.11.0/24"]

  # Cost saver: one shared NAT Gateway instead of one per AZ. If AZ-a goes
  # down, AZ-b pods lose outbound internet — acceptable trade-off for a demo.
  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  # EKS uses these tags to discover where to place external/internal load
  # balancers. The ALB Ingress Controller in Phase 4 will look for them.
  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }
}
