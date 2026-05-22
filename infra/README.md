# WOMS infrastructure (Terraform)

All AWS resources for WOMS are declared in this folder. Apply this once per
work session, do your thing on the cluster, then `terraform destroy` at the
end of the session to stop the meter running.

## File layout

| File | Purpose |
|---|---|
| `main.tf` | Terraform + AWS provider config, default tags |
| `variables.tf` | Input variable declarations |
| `outputs.tf` | Outputs (RDS endpoint, EKS cluster name, etc.) |
| `terraform.tfvars` | Actual values for variables (gitignored — may hold secrets) |
| `terraform.tfvars.example` | Template you copy to `terraform.tfvars` |
| `vpc.tf` | (added in Phase 2 step 3) Network — VPC, subnets, NAT, IGW |
| `rds.tf` | (added in Phase 2 step 4) PostgreSQL on RDS |
| `elasticache.tf` | (added in Phase 2 step 5) Redis on ElastiCache |
| `eks.tf` | (added in Phase 2 step 6+7) EKS cluster + node group |

## Day-to-day commands

```powershell
# from repo root:
cd infra

# First time only (or after adding a new provider in main.tf):
terraform init

# Preview what would change — ALWAYS run before apply:
terraform plan

# Apply changes (you'll see a prompt asking for "yes"):
terraform apply

# Tear down everything in this folder:
terraform destroy
```

## Cost discipline

EKS control plane bills $0.10 per hour as long as it exists. NAT Gateway is
another $0.045 per hour. If you leave both running 24/7 for three weeks
that's ~$73 from EKS plus ~$22 from NAT.

**Routine:** `terraform apply` at the start of a work session, `terraform
destroy` when you stop. RDS data is wiped between destroys — for the demo
you'll seed it via Alembic each time, which is fine because the dataset is
small.

## State

`terraform.tfstate` is the source of truth for "what exists." Do not delete
it unless you really want to abandon Terraform and start over (in which
case you must also manually delete every resource via the AWS Console).

If your laptop dies and you lose the state file, see the runbook in
`docs/ARCHITECTURE.md` (TBD) or ask the team — recovery means re-importing
each resource or, more realistically, deleting everything via the Console.
