# Deploy / collaboration guide

How the WOMS AWS deployment works day-to-day. Read this before opening
a PR to `main`, before running `terraform apply`, or before tearing down
the cluster.

## TL;DR rules

1. **Tell the team before `terraform apply`** — the meter starts (~$0.22/hr).
2. **Tell the team after `terraform destroy`** — meter stops, but the demo
   URL is dead until someone re-applies.
3. **Tell the team before merging to `main`** when the cluster is up — the
   merge triggers `deploy.yml`, which will roll the new image. If multiple
   people merge concurrently the second deploy waits for the first.
4. **Don't push to `main` directly** — branch protection blocks it. Always
   feature branch + PR.
5. **Don't commit secrets** — `terraform.tfvars` is gitignored on purpose.

## Workflow: shipping a code change

```
1. git checkout main && git pull
2. git checkout -b feat/whatever       (or fix/, chore/, docs/)
3. ... write code ...
4. git push origin feat/whatever
5. Open PR on GitHub: base=main ← compare=feat/whatever
6. CI runs (lint + tests). Two checks must pass.
7. Notify team: "Going to merge #N — cluster is/isn't up"
8. Merge PR via GitHub UI (squash or merge commit, team preference)
9. Watch `Deploy to EKS` workflow in Actions tab if cluster is up
```

## How to know if deploy succeeded

After merging to `main` with the cluster up:

1. **GitHub Actions** — repo → Actions tab → top of list, look for
   `Deploy to EKS` against your commit SHA.
   - Green check ✅ + duration ~5–8 min → success
   - Red X ❌ → click the run, find the failed step, share the log
2. **Browser smoke-test** — open the CloudFront URL (ask team owner),
   the WOMS login page should load and admin login should still work.
3. **kubectl** (optional, if you have cluster access):
   ```
   kubectl rollout history deployment/backend -n woms
   ```
   Latest revision should be the most recent timestamp.

If you don't see `Deploy to EKS` running at all after a merge, the cluster
is probably down — `deploy.yml` still runs but fails at `Update kubeconfig`.
That's expected and harmless.

## If deploy.yml fails

- **Don't panic** — Kubernetes does rolling updates, the old image is
  still serving traffic. The site doesn't go down on deploy failure.
- Click into the failed step, read the log.
- Most common causes:
  - **Cluster is down** — re-apply infra first, then re-run the workflow
    (Actions tab → click run → Re-run failed jobs).
  - **Image build error** — fix the bug in a new PR.
  - **`kubectl set image` timeout** — pod failed to become Ready. Look at
    `kubectl logs deployment/<name> -n woms`.
- After fixing, push to the feature branch → CI passes → merge again →
  new `deploy.yml` run.

## Demo URL + credentials

Both are out-of-band — not in the repo for security reasons.

- **URL**: ask the person who last ran `terraform apply`. It's the
  CloudFront hostname (`https://d…cloudfront.net`) printed by:
  ```
  terraform -chdir=infra output -raw cloudfront_url
  ```
- **Admin password**: ask the team owner. It lives in
  `infra/terraform.tfvars` (gitignored, local-only). Stable across
  destroy/apply cycles since it's set explicitly there.
- **Admin username**: `admin` (always).

## Cost discipline

While the cluster is up:

| Resource | $/hr |
|---|---|
| EKS control plane | 0.10 |
| 2× t3.small worker nodes | 0.04 |
| RDS db.t3.micro | 0.02 |
| ElastiCache cache.t3.micro | 0.02 |
| NAT Gateway | 0.045 |
| ALB | 0.022 |
| **Total** | **~0.24/hr ($5.30/day)** |

Out of $200 (or $300 with task credits) for the project, leaving it up
24/7 burns the budget in ~30 days. So:

- **Default state**: cluster is **down**. Don't apply unless you have a
  reason (demo, integration testing, dev that needs the real DB).
- **Demo day**: `terraform apply` the morning of, `destroy` the evening.
- **Always check Budgets in AWS console** if unsure whether something is
  still running.

## "Cluster up / down" lifecycle

### Bringing it up (~30 min)

1. Tell team: "Bringing the cluster up for X reason."
2. `cd infra && terraform apply` (~15 min)
3. `aws eks update-kubeconfig --region ap-northeast-1 --name woms-dev`
4. Re-create the Secret (DATABASE_URL / REDIS_URL / JWT_SECRET / ADMIN_PASSWORD)
   — see `k8s/README.md` for the exact command.
5. `./k8s/apply-all.ps1`
6. Wait for Ingress to get an ALB hostname:
   `kubectl get ingress woms -n woms -w`
7. Put the ALB hostname into `infra/terraform.tfvars` as `alb_dns_name`.
8. `cd infra && terraform apply` again (provisions CloudFront, ~15 min).
9. Tell team: "Cluster is up at <CloudFront URL>."

### Bringing it down (~15 min)

1. Tell team: "Tearing down."
2. Comment out `alb_dns_name = "..."` in `infra/terraform.tfvars`.
3. `cd infra && terraform destroy`
4. Verify in AWS console that NAT Gateway / EKS / RDS / ElastiCache are
   all gone.
5. Tell team: "Cluster is destroyed. Demo URL is dead until next apply."

## Files you shouldn't touch without coordination

- `infra/*.tf` — owned by infra lead; PRs welcome but discuss first
- `k8s/*.yaml` — same as above, plus needs running cluster to test
- `.github/workflows/deploy.yml` — changing this can break every future
  deploy; review carefully

## Who to ping when

- **Cluster won't come up / weird Terraform error** → infra lead
- **CI green but website acting up** → backend or frontend owner
  depending on the bug
- **Cost overrun warning email from AWS** → everyone, immediately
