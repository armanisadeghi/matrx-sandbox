# ─── Matrx Sandbox Infrastructure ─────────────────────────────────────────────
#
# This Terraform configuration creates:
# - S3 bucket for hot/cold sandbox storage
# - EC2 instance for running Docker sandbox containers
# - IAM roles and policies for S3 and CloudWatch access
# - Security groups for the sandbox host
#
# Usage:
#   cd infra/terraform
#   cp terraform.tfvars.example terraform.tfvars  # edit with your values
#   terraform init
#   terraform plan
#   terraform apply
