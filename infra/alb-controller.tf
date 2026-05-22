# IAM role for the AWS Load Balancer Controller (running as a pod inside the
# cluster). Uses the IRSA pattern: the K8s service account
# `kube-system/aws-load-balancer-controller` can assume this role thanks to
# the EKS OIDC provider trust we set up in eks.tf.
#
# The controller needs broad EC2 / ELB / WAF permissions because it provisions
# ALBs, target groups, listener rules, security groups, etc. on your behalf.
# Don't shortcut by hand-attaching AdministratorAccess — the curated policy
# below is exactly what the controller needs and nothing more.

module "alb_controller_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${var.project}-${var.environment}-alb-controller"

  # The IAM submodule ships with a curated policy that mirrors the official
  # iam_policy.json from kubernetes-sigs/aws-load-balancer-controller.
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}
