# Arman's Tasks

Manual setup steps that cannot be done from within this codebase. Complete these
in order. Each section tells you exactly what to do, what options to select, and
what values to bring back.

---

## Status

| # | Task | Status |
|---|------|--------|
| 1 | AWS Account Setup | TODO |
| 2 | Create S3 Bucket | TODO |
| 3 | Create EC2 Key Pair | TODO |
| 4 | Configure AWS CLI Locally | TODO |
| 5 | Create ECR Repository | TODO |
| 6 | Deploy First EC2 Instance | TODO |
| 7 | Build & Push Sandbox Image | TODO |
| 8 | End-to-End Test | TODO |

---

## 1. AWS Account Setup

If you already have an AWS account with admin access, skip to step 2.

1. Go to https://aws.amazon.com/ and click "Create an AWS Account"
2. Use your work email, set the account name to "AI Matrx" (or similar)
3. Choose **Personal** or **Business** account type
4. Add a payment method (credit card required)
5. Choose the **Basic Support (Free)** plan
6. After account creation, go to **IAM** in the AWS Console
7. Create an IAM user for programmatic access:
   - Go to IAM → Users → Create User
   - Name: `matrx-admin`
   - Check **"Provide user access to the AWS Management Console"**
   - Select **"I want to create an IAM user"**
   - For permissions, choose **"Attach policies directly"**
   - Attach: `AdministratorAccess` (for initial setup — we'll scope this down later)
   - Complete creation
8. Go to the user → Security credentials → Create access key
   - Use case: "Command Line Interface (CLI)"
   - Save the **Access Key ID** and **Secret Access Key** — you'll need them in step 4

**Values to save:**
```
AWS_ACCOUNT_ID=____________
AWS_ACCESS_KEY_ID=____________
AWS_SECRET_ACCESS_KEY=____________
```

---

## 2. Create S3 Bucket

1. Go to AWS Console → S3 → Create bucket
2. Settings:
   - **Bucket name**: `matrx-sandbox-storage-<your-unique-suffix>` (must be globally unique; use something like `matrx-sandbox-storage-prod-2024`)
   - **Region**: `us-east-1` (or your preferred region — just be consistent everywhere)
   - **Object Ownership**: ACLs disabled (recommended)
   - **Block all public access**: ✅ ON (keep all four boxes checked)
   - **Bucket Versioning**: Enable
   - **Default encryption**: Server-side encryption with Amazon S3 managed keys (SSE-S3)
   - **Bucket Key**: Enable
3. Click Create bucket

**Values to bring back** (add to your `.env` file in the repo):
```
MATRX_S3_BUCKET=matrx-sandbox-storage-<your-suffix>
MATRX_S3_REGION=us-east-1
```

---

## 3. Create EC2 Key Pair

This lets you SSH into the sandbox host EC2 instance.

1. Go to AWS Console → EC2 → Key Pairs (left sidebar under "Network & Security")
2. Click "Create key pair"
3. Settings:
   - **Name**: `matrx-sandbox-key`
   - **Key pair type**: RSA
   - **Private key file format**: `.pem` (for Mac/Linux) or `.ppk` (for PuTTY on Windows)
4. Click Create — it will download the `.pem` file
5. Move the file to a safe location and set permissions:
   ```bash
   mv ~/Downloads/matrx-sandbox-key.pem ~/.ssh/
   chmod 400 ~/.ssh/matrx-sandbox-key.pem
   ```

**Values to save:**
```
EC2_KEY_PAIR_NAME=matrx-sandbox-key
```

---

## 4. Configure AWS CLI Locally

1. Install the AWS CLI if you don't have it:
   ```bash
   # macOS
   brew install awscli

   # Or download from https://aws.amazon.com/cli/
   ```

2. Configure it with the credentials from step 1:
   ```bash
   aws configure
   ```
   Enter:
   - **AWS Access Key ID**: (from step 1)
   - **AWS Secret Access Key**: (from step 1)
   - **Default region name**: `us-east-1`
   - **Default output format**: `json`

3. Verify it works:
   ```bash
   aws sts get-caller-identity
   ```
   You should see your account ID and user ARN.

4. Verify S3 access:
   ```bash
   aws s3 ls s3://matrx-sandbox-storage-<your-suffix>/
   ```

---

## 5. Create ECR Repository

ECR (Elastic Container Registry) stores the sandbox Docker image so EC2
instances can pull it.

1. Go to AWS Console → ECR → Create repository
2. Settings:
   - **Visibility**: Private
   - **Repository name**: `matrx-sandbox`
   - **Tag immutability**: Disabled
   - **Scan on push**: Enabled
   - **Encryption**: AES-256
3. Click Create repository

4. Note the repository URI — it looks like:
   `<account-id>.dkr.ecr.<region>.amazonaws.com/matrx-sandbox`

**Values to save:**
```
ECR_REPO_URI=<account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox
```

---

## 6. Deploy Infrastructure with Terraform

1. Install Terraform if you don't have it:
   ```bash
   # macOS
   brew install terraform

   # Or download from https://developer.hashicorp.com/terraform/downloads
   ```

2. Configure the Terraform variables:
   ```bash
   cd infra/terraform
   cp terraform.tfvars.example terraform.tfvars
   ```

3. Edit `terraform.tfvars` with your values:
   ```hcl
   s3_bucket_name    = "matrx-sandbox-storage-<your-suffix>"
   ec2_key_pair_name = "matrx-sandbox-key"
   # ec2_instance_type = "t3.xlarge"   # optional, default is fine
   ```

4. Initialize and apply:
   ```bash
   terraform init
   terraform plan        # review what will be created
   terraform apply       # type "yes" to confirm
   ```

5. Save the outputs:
   ```bash
   terraform output
   ```

**Values to save:**
```
EC2_INSTANCE_ID=____________
EC2_PUBLIC_IP=____________
```

---

## 7. Build & Push Sandbox Image

Once you have ECR and EC2 running:

1. Authenticate Docker to ECR:
   ```bash
   aws ecr get-login-password --region us-east-1 | \
     docker login --username AWS --password-stdin \
     <account-id>.dkr.ecr.us-east-1.amazonaws.com
   ```

2. Build the sandbox image:
   ```bash
   cd sandbox-image
   docker build -t matrx-sandbox:latest .
   ```

3. Tag and push to ECR:
   ```bash
   docker tag matrx-sandbox:latest \
     <account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox:latest

   docker push \
     <account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox:latest
   ```

4. SSH into the EC2 instance and pull the image:
   ```bash
   ssh -i ~/.ssh/matrx-sandbox-key.pem ec2-user@<EC2_PUBLIC_IP>

   # On the EC2 instance:
   aws ecr get-login-password --region us-east-1 | \
     docker login --username AWS --password-stdin \
     <account-id>.dkr.ecr.us-east-1.amazonaws.com

   docker pull <account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox:latest
   docker tag <account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox:latest \
     matrx-sandbox:latest
   ```

---

## 8. End-to-End Test

Run these on the EC2 instance to verify everything works:

1. Start the orchestrator:
   ```bash
   # On EC2 instance
   cd /home/ec2-user
   pip3.11 install matrx-orchestrator  # or clone repo and install
   python3.11 -m orchestrator.main
   ```

2. In another terminal, create a test sandbox:
   ```bash
   curl -X POST http://localhost:8000/sandboxes \
     -H "Content-Type: application/json" \
     -d '{"user_id": "test-user-1"}'
   ```

3. Execute a command:
   ```bash
   curl -X POST http://localhost:8000/sandboxes/<sandbox-id>/exec \
     -H "Content-Type: application/json" \
     -d '{"command": "whoami && pwd && ls -la"}'
   ```

4. Destroy the sandbox:
   ```bash
   curl -X DELETE "http://localhost:8000/sandboxes/<sandbox-id>?graceful=true"
   ```

5. Verify hot storage was synced:
   ```bash
   aws s3 ls s3://matrx-sandbox-storage-<suffix>/users/test-user-1/hot/
   ```

---

## Environment Variables Summary

After completing all steps, your `.env` file should contain:

```bash
# AWS
MATRX_S3_BUCKET=matrx-sandbox-storage-<your-suffix>
MATRX_S3_REGION=us-east-1

# Orchestrator
MATRX_HOST=0.0.0.0
MATRX_PORT=8000
MATRX_DEBUG=false
MATRX_SANDBOX_IMAGE=matrx-sandbox:latest

# ECR (for CI/CD)
ECR_REPO_URI=<account-id>.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox

# EC2
EC2_KEY_PAIR_NAME=matrx-sandbox-key
EC2_PUBLIC_IP=<from terraform output>
```
