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

# Image tag for templated manifests. deploy.yml pushes both :<sha> and
# :latest, so the default picks up the most recent build. Override with
# $env:IMAGE_TAG="<sha>" to pin a specific build.
$IMAGE_TAG  = if ($env:IMAGE_TAG) { $env:IMAGE_TAG } else { "latest" }

Write-Host "ECR registry: $ECR_URI" -ForegroundColor Cyan
Write-Host "Image tag:    $IMAGE_TAG" -ForegroundColor Cyan

function Apply-Plain($file) {
  Write-Host "Applying $file..." -ForegroundColor Yellow
  kubectl apply -f $file
}

function Apply-Templated($file) {
  Write-Host "Applying $file (templated)..." -ForegroundColor Yellow
  $content = Get-Content $file -Raw
  $content = $content -replace '\$\{ECR_URI\}', $ECR_URI
  $content = $content -replace '\$\{IMAGE_TAG\}', $IMAGE_TAG
  $content | kubectl apply -f -
}

# 1. Namespace + non-secret config + services (no image refs).
Apply-Plain "k8s/00-namespace.yaml"
Apply-Plain "k8s/10-configmap.yaml"
Apply-Plain "k8s/31-backend-service.yaml"
Apply-Plain "k8s/51-frontend-service.yaml"

# 2. Alembic schema migration — must finish before anything that reads/writes
#    the DB (seed-admin and the backend deployment both do).
Apply-Templated "k8s/20-migration-job.yaml"
Write-Host "Waiting for migration to finish..." -ForegroundColor Cyan
kubectl wait --for=condition=complete --timeout=180s job/woms-migrate -n woms

# 3. Seed the default demo admin once the schema is in place.
Apply-Templated "k8s/25-seed-admin-job.yaml"
Write-Host "Waiting for admin seed to finish..." -ForegroundColor Cyan
kubectl wait --for=condition=complete --timeout=60s job/woms-seed-admin -n woms

# 4. App workloads.
Apply-Templated "k8s/30-backend-deployment.yaml"
Apply-Templated "k8s/40-worker-deployment.yaml"
Apply-Templated "k8s/50-frontend-deployment.yaml"

# 5. Ingress (provisions the ALB on the AWS side — takes 3–5 min).
Apply-Plain "k8s/60-ingress.yaml"

Write-Host "`nAll manifests applied." -ForegroundColor Green
Write-Host "Demo admin: username=admin" -ForegroundColor Green
Write-Host "Password:   terraform -chdir=infra output -raw admin_password" -ForegroundColor Green
Write-Host "Watch pods come up with: kubectl get pods -n woms -w"
