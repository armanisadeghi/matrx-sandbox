# Arman's Tasks

Manual setup steps that only Arman can do (account creation, credentials,
local tool installation). Other tasks have been delegated:
- **Coding tasks** (tests, CI/CD, refactoring): → `CLAUDE_CODE_AGENT_TASKS.md`

---

## Status

All initial setup tasks are **DONE**. The system is operational.

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | AWS Account Setup | DONE | IAM user `matrx-admin` created |
| 2 | Create S3 Bucket | DONE | `matrx-sandbox-storage-prod-2024` in us-east-1 |
| 3 | Create EC2 Key Pair | DONE | `matrx-sandbox-key` — .pem at ~/Code/secrets/ |
| 4 | Configure AWS CLI | DONE | Verified via `aws sts get-caller-identity` |
| 5 | Create ECR Repository | DONE | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| 6 | Deploy Infrastructure | DONE | EC2 `i-084f757c1e47d4efb` at `44.204.37.36` |
| 7 | Build Sandbox Image | DONE | Built on EC2 (2.22GB). ECR push via CI/CD. |
| 8 | End-to-End Test | DONE | Sandbox create/exec/destroy all working |

---

## URGENT: Rotate AWS Credentials

Your AWS access key was exposed in a chat message. You must rotate it immediately:

1. Go to **IAM → Users → matrx-admin → Security credentials**
2. Click **Create access key** (new one)
3. Save the new key ID and secret
4. **Delete the old key** (the one that was exposed)
5. Update your local `~/.aws/credentials` and `.env` with the new values
6. Update the EC2 instance if it uses these credentials directly

---

## Remaining Tasks for Arman

### A1. Restrict Terraform Security Groups

The Terraform config now requires explicit CIDR blocks for SSH and API access.
Update your `infra/terraform/terraform.tfvars`:

```hcl
ssh_cidr_blocks = ["YOUR_IP/32"]    # e.g., ["203.0.113.50/32"]
api_cidr_blocks = ["YOUR_IP/32"]    # Same as above for now
ecr_repo_arn    = "arn:aws:ecr:us-east-1:872515272894:repository/matrx-sandbox"
```

Then re-apply:
```bash
cd infra/terraform
terraform plan
terraform apply
```

### A2. Set Up GitHub Secrets for CI/CD

The CI/CD pipeline needs these secrets in the GitHub repository settings
(Settings → Secrets and variables → Actions):

| Secret Name | Value |
|-------------|-------|
| `AWS_ACCESS_KEY_ID` | Your new access key (after rotation) |
| `AWS_SECRET_ACCESS_KEY` | Your new secret key (after rotation) |
| `ECR_REPO_URI` | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |

### A3. Rebuild Sandbox Image on EC2

After code review fixes are merged, rebuild the sandbox image on EC2:

```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@44.204.37.36

# Pull latest code
cd ~/matrx-sandbox && git pull origin main

# Rebuild
docker build -t matrx-sandbox:latest sandbox-image/

# Restart orchestrator
# (stop current one, then restart with updated code)
```

---

## Infrastructure Reference

| Resource | Value |
|----------|-------|
| AWS Account ID | `872515272894` |
| S3 Bucket | `matrx-sandbox-storage-prod-2024` |
| S3 Region | `us-east-1` |
| EC2 Instance | `i-084f757c1e47d4efb` |
| EC2 Public IP | `44.204.37.36` |
| EC2 Key Pair | `matrx-sandbox-key` |
| ECR Repository | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| IAM Role | `matrx-sandbox-host-dev` |
