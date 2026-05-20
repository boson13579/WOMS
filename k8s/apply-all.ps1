# Apply every YAML in k8s/ to the cluster, substituting ${ECR_URI} with the
# account-specific ECR registry. Run from repo root:
#
#   ./k8s/apply-all.ps1
#
# This is the deploy-the-first-time script. For rolling updates after
# you've already deployed, prefer:
#   kubectl set image deployment/backend backend=<ecr>/woms-backend:v0.2 -n woms

$ErrorActionPreference = "Stop"

$AWS_REGION = "ap-northeast-1"
$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)
$ECR_URI    = "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

Write-Host "ECR registry: $ECR_URI" -ForegroundColor Cyan

# Files that DON'T need substitution (no image refs).
$plainFiles = @(
  "k8s/00-namespace.yaml",
  "k8s/10-configmap.yaml",
  "k8s/31-backend-service.yaml",
  "k8s/51-frontend-service.yaml"
)
foreach ($f in $plainFiles) {
  Write-Host "Applying $f..." -ForegroundColor Yellow
  kubectl apply -f $f
}

# Files that reference an image — substitute ${ECR_URI} on the fly via stdin.
$templatedFiles = @(
  "k8s/20-migration-job.yaml",
  "k8s/30-backend-deployment.yaml",
  "k8s/40-worker-deployment.yaml",
  "k8s/50-frontend-deployment.yaml"
)
foreach ($f in $templatedFiles) {
  Write-Host "Applying $f (templated)..." -ForegroundColor Yellow
  (Get-Content $f -Raw) -replace '\$\{ECR_URI\}', $ECR_URI | kubectl apply -f -
}

Write-Host "`nAll manifests applied. Watch pods come up with:" -ForegroundColor Green
Write-Host "  kubectl get pods -n woms -w"
