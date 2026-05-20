# EKS — managed Kubernetes cluster + a single managed node group.
#
# Uses the official terraform-aws-modules/eks/aws module. The raw resources
# would be ~30 IAM policies, addons, OIDC providers, launch templates and
# is a known footgun field; the module bakes the well-known defaults in.
#
# Hourly cost while this is applied:
#   - Control plane:        $0.10/hr  ($2.40/day)
#   - 2x t3.small on-demand: $0.0416/hr ($1.00/day)
# Plus existing VPC/RDS/Redis. Total ~$5/day if you forget to destroy.

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "${var.project}-${var.environment}"
  cluster_version = "1.30"

  # Public endpoint so our laptop's kubectl can reach the API server.
  # Authentication is still via IAM — the endpoint being public just means
  # the API server is reachable; you still need credentials to call it.
  cluster_endpoint_public_access  = true
  cluster_endpoint_private_access = true

  # Auto-grant cluster-admin to the IAM principal running `terraform apply`
  # (woms-admin). Without this you'd see "Unauthorized" the first time you
  # run kubectl, and need to bootstrap aws-auth ConfigMap manually.
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Cluster add-ons — installed and version-managed by AWS. Without these,
  # nothing networks correctly. eks-pod-identity-agent is the modern
  # replacement for IRSA service account annotations.
  cluster_addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = {}
    eks-pod-identity-agent = {}
  }

  eks_managed_node_groups = {
    default = {
      # Amazon Linux 2023 — current default for new clusters.
      ami_type = "AL2023_x86_64_STANDARD"

      instance_types = ["t3.small"]
      capacity_type  = "ON_DEMAND"

      min_size     = 1
      max_size     = 4
      desired_size = 2

      # Disk for container images + ephemeral storage. 20 GB is plenty for
      # WOMS images (~250 MB backend + ~50 MB frontend); generous headroom
      # for the OS, kubelet logs, etc.
      disk_size = 20

      labels = {
        role = "general"
      }
    }
  }
}
