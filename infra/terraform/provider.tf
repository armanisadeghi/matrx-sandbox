terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — uncomment and configure for team use
  # backend "s3" {
  #   bucket = "matrx-terraform-state"
  #   key    = "sandbox/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "matrx-sandbox"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
