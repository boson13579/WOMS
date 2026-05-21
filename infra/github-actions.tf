# OIDC trust + IAM role used by GitHub Actions to push to ECR and update
# Kubernetes Deployments without storing long-lived access keys.
#
# How it works:
#   1. GitHub Actions runs and gets a short-lived OIDC token from GitHub.
#   2. The job calls `aws sts assume-role-with-web-identity` to swap that
#      token for a 1-hour AWS credentials set, tied to this IAM role.
#   3. The role's trust policy ONLY accepts tokens whose `sub` claim
#      matches the repo specified below — fork-safe.
#
# No access keys are stored in GitHub Secrets — only the role ARN, which
# is not a credential by itself.

variable "github_repo" {
  description = "GitHub repo allowed to assume the GHA role, format owner/name."
  type        = string
  default     = "boson13579/WOMS"
}

# Fetches GitHub's OIDC cert so we can pin the SHA-1 thumbprint without
# hard-coding it (the value rotates occasionally).
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "github_actions" {
  name = "${var.project}-${var.environment}-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # The `sub` claim looks like `repo:OWNER/NAME:ref:refs/heads/main`
        # or `repo:OWNER/NAME:environment:prod`. Use StringLike with a
        # wildcard to allow any branch / environment in the right repo,
        # but reject other repos (incl. forks).
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })
}

# Minimal AWS-side permissions: pull ECR auth, push/pull woms-* images,
# read EKS cluster info so kubectl can connect.
resource "aws_iam_role_policy" "github_actions" {
  name = "${var.project}-${var.environment}-github-actions"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "ECRPushPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:DescribeImages",
          "ecr:DescribeRepositories"
        ]
        Resource = [
          "arn:aws:ecr:${var.aws_region}:*:repository/woms-*"
        ]
      },
      {
        Sid    = "EKSReadForKubectl"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters"
        ]
        Resource = "*"
      }
    ]
  })
}

# Cluster-side authorisation: map this IAM role into Kubernetes as an
# "Edit" user scoped to the `woms` namespace. Just enough to run
# `kubectl set image deployment/<x>` and `kubectl rollout status`, but
# nothing privileged like CRDs / nodes / kube-system.
resource "aws_eks_access_entry" "github_actions" {
  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.github_actions.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "github_actions" {
  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.github_actions.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = ["woms"]
  }

  depends_on = [aws_eks_access_entry.github_actions]
}
