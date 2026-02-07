# Arman's Tasks

## Completed

All initial infrastructure is deployed and tested. See Infrastructure Reference below.

| # | Task | Notes |
|---|------|-------|
| 1 | AWS Account Setup | IAM user `matrx-admin`, access key generated |
| 2 | S3 Bucket | `matrx-sandbox-storage-prod-2024` (us-east-1) |
| 3 | EC2 Key Pair | `matrx-sandbox-key` at ~/Code/secrets/ |
| 4 | AWS CLI | Configured and verified |
| 5 | ECR Repository | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| 6 | Terraform Deploy | EC2 `i-084f757c1e47d4efb` at `44.204.37.36` |
| 7 | Sandbox Image | Built on EC2 (2.22GB) |
| 8 | E2E Test | Sandbox create/exec/destroy verified |

---

## Remaining

### A1. Restrict Security Groups

Update `infra/terraform/terraform.tfvars` with your IP:

```hcl
ssh_cidr_blocks = ["YOUR_IP/32"]
api_cidr_blocks = ["YOUR_IP/32"]
ecr_repo_arn    = "arn:aws:ecr:us-east-1:872515272894:repository/matrx-sandbox"
```

Then: `cd infra/terraform && terraform plan && terraform apply`

### A2. Set Up GitHub Secrets for CI/CD

Go to repo Settings → Secrets → Actions and add:

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | Your access key |
| `AWS_SECRET_ACCESS_KEY` | Your secret key |
| `ECR_REPO_URI` | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |

### A3. Rebuild Sandbox Image on EC2 (after merging code fixes)

```bash
ssh -i ~/Code/secrets/matrx-sandbox-key.pem ec2-user@44.204.37.36
cd ~/matrx-sandbox && git pull origin main
docker build -t matrx-sandbox:latest sandbox-image/
```

---

## Infrastructure Reference

| Resource | Value |
|----------|-------|
| AWS Account | `872515272894` |
| S3 Bucket | `matrx-sandbox-storage-prod-2024` |
| EC2 Instance | `i-084f757c1e47d4efb` at `44.204.37.36` |
| ECR Repo | `872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox` |
| Key Pair | `matrx-sandbox-key` (~/Code/secrets/) |
| IAM Role | `matrx-sandbox-host-dev` |
