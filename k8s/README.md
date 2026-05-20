# Kubernetes manifests for WOMS

Deploys the three WOMS services (FastAPI backend, Celery worker, React
frontend served by Nginx) plus a one-shot Alembic migration Job to the EKS
cluster provisioned by `infra/`.

## Prerequisites

- `kubectl` configured against the cluster (see `infra/README.md` for
  `aws eks update-kubeconfig`).
- The cluster + RDS + Redis are all up (`terraform apply` in `infra/`).

## File layout

Files are prefixed with numbers so `kubectl apply -f k8s/` sorts them in
dependency order:

| File | What it does |
| --- | --- |
| `00-namespace.yaml` | Create the `woms` namespace |
| `10-configmap.yaml` | Non-secret env (APP_ENV, LOG_LEVEL, CORS_ORIGINS) |
| `20-migration-job.yaml` | One-shot `alembic upgrade head` Job |
| `25-seed-admin-job.yaml` | One-shot Job that creates the demo root admin (idempotent) |
| `30-backend-deployment.yaml` | FastAPI pods (2 replicas) |
| `31-backend-service.yaml` | ClusterIP `backend.woms.svc` |
| `40-worker-deployment.yaml` | Celery worker pod (1 replica) |
| `50-frontend-deployment.yaml` | Nginx pods (2 replicas) |
| `51-frontend-service.yaml` | ClusterIP `frontend.woms.svc` |
| `60-ingress.yaml` | ALB Ingress (path-based routing: /api/* → backend, else → frontend) |

## Demo admin

After a fresh deploy, `25-seed-admin-job.yaml` runs and creates a root user
with these credentials so reviewers can log in without registering:

```text
username: admin
password: testpassword123
```

The Job is idempotent — it checks for an existing `admin` user and exits 0
without changes. For production, delete this Job and create the admin
manually via `kubectl exec` into a backend pod.

## Deploy (first time)

```powershell
# 1. Namespace + non-secret config
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-configmap.yaml

# 2. Secret built from Terraform outputs.
#    Run from repo root with the cluster + RDS already applied.
$DATABASE_URL = (terraform -chdir=infra output -raw database_url)
$REDIS_URL    = (terraform -chdir=infra output -raw redis_url)
$JWT_SECRET   = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})

kubectl create secret generic woms-secrets `
  --namespace woms `
  --from-literal=DATABASE_URL="$DATABASE_URL" `
  --from-literal=REDIS_URL="$REDIS_URL" `
  --from-literal=JWT_SECRET="$JWT_SECRET"

# 3. Run migrations first — backend pods won't start cleanly without them.
kubectl apply -f k8s/20-migration-job.yaml
kubectl wait --for=condition=complete --timeout=180s job/woms-migrate -n woms

# 4. Deploy the apps
kubectl apply -f k8s/30-backend-deployment.yaml
kubectl apply -f k8s/31-backend-service.yaml
kubectl apply -f k8s/40-worker-deployment.yaml
kubectl apply -f k8s/50-frontend-deployment.yaml
kubectl apply -f k8s/51-frontend-service.yaml

# 5. Watch pods
kubectl get pods -n woms -w
```

## Redeploy with a new image tag

```powershell
# Update the image tag in 30-backend-deployment.yaml / 50-frontend-deployment.yaml
# Then:
kubectl apply -f k8s/

# Or roll out a fresh tag without editing files:
kubectl set image deployment/backend backend=<ECR>/woms-backend:v0.2 -n woms
kubectl rollout status deployment/backend -n woms
```

## Tear down (without destroying infra)

```powershell
kubectl delete namespace woms
```

This wipes every WOMS resource but leaves the cluster intact.

## Tear down the cluster too

Go back to `infra/` and run `terraform destroy` — that takes ~10 minutes.
