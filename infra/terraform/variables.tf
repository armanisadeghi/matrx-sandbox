variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name prefix for resource naming"
  type        = string
  default     = "matrx-sandbox"
}

# ─── S3 ──────────────────────────────────────────────────────────────────────

variable "s3_bucket_name" {
  description = "Name of the S3 bucket for sandbox storage"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.s3_bucket_name))
    error_message = "S3 bucket name must be 3-63 characters, start and end with a lowercase letter or number, and contain only lowercase letters, numbers, hyphens, and periods."
  }
}

# ─── EC2 ─────────────────────────────────────────────────────────────────────

variable "ec2_instance_type" {
  description = "EC2 instance type for sandbox hosts"
  type        = string
  default     = "t3.xlarge" # 4 vCPU, 16 GB RAM — good for 2-4 concurrent sandboxes

  validation {
    condition     = can(regex("^[a-z]", var.ec2_instance_type))
    error_message = "EC2 instance type must start with a letter."
  }
}

variable "ec2_ami_id" {
  description = "AMI ID for sandbox host (Amazon Linux 2023 or Ubuntu 22.04). Leave empty for latest Amazon Linux 2023."
  type        = string
  default     = ""
}

variable "ec2_key_pair_name" {
  description = "EC2 key pair name for SSH access"
  type        = string
  default     = ""
}

variable "ec2_use_spot" {
  description = "Use EC2 Spot Instances for cost savings"
  type        = bool
  default     = true
}

variable "ec2_spot_max_price" {
  description = "Maximum hourly price for Spot Instances (empty = on-demand price)"
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "VPC ID to launch instances in. Leave empty to use default VPC."
  type        = string
  default     = ""
}

variable "subnet_id" {
  description = "Subnet ID to launch instances in. Leave empty for default."
  type        = string
  default     = ""
}

# ─── Security ─────────────────────────────────────────────────────────────────

variable "ssh_cidr_blocks" {
  description = "CIDR blocks allowed SSH access (e.g. your IP). Empty = no SSH access."
  type        = list(string)
  default     = []
}

variable "api_cidr_blocks" {
  description = "CIDR blocks allowed API access. Empty = no external API access."
  type        = list(string)
  default     = []
}

variable "ecr_repo_arn" {
  description = "ARN of the ECR repository for sandbox images"
  type        = string
  default     = ""
}
