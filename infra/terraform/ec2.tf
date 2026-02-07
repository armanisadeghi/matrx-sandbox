# ─── Data Sources ─────────────────────────────────────────────────────────────

# Latest Amazon Linux 2023 AMI (used if ec2_ami_id is not provided)
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Default VPC (used if vpc_id is not provided)
data "aws_vpc" "default" {
  count   = var.vpc_id == "" ? 1 : 0
  default = true
}

locals {
  ami_id = var.ec2_ami_id != "" ? var.ec2_ami_id : data.aws_ami.amazon_linux.id
  vpc_id = var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default[0].id
}

# ─── Security Group ──────────────────────────────────────────────────────────

resource "aws_security_group" "sandbox_host" {
  name_prefix = "${var.project_name}-host-"
  description = "Security group for Matrx sandbox host EC2 instances"
  vpc_id      = local.vpc_id

  # SSH access (restrict to your IP in production)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # TODO: restrict in production
    description = "SSH access"
  }

  # Orchestrator API (internal only in production)
  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # TODO: restrict in production
    description = "Orchestrator API"
  }

  # All outbound (containers need internet for APIs, package installs, etc.)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound"
  }

  tags = {
    Name = "${var.project_name}-host-sg"
  }
}

# ─── EC2 Instance ─────────────────────────────────────────────────────────────

resource "aws_instance" "sandbox_host" {
  ami                    = local.ami_id
  instance_type          = var.ec2_instance_type
  key_name               = var.ec2_key_pair_name != "" ? var.ec2_key_pair_name : null
  iam_instance_profile   = aws_iam_instance_profile.sandbox_host.name
  vpc_security_group_ids = [aws_security_group.sandbox_host.id]
  subnet_id              = var.subnet_id != "" ? var.subnet_id : null

  # Root volume
  root_block_device {
    volume_size = 50  # GB — space for Docker images + container layers
    volume_type = "gp3"
    encrypted   = true
  }

  # User data script to install Docker and dependencies
  user_data = file("${path.module}/../scripts/bootstrap-host.sh")

  # Enable instance metadata service v2 (more secure)
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # Allow containers to reach IMDS
  }

  tags = {
    Name = "${var.project_name}-host-${var.environment}"
  }
}

# ─── Spot Instance Alternative ────────────────────────────────────────────────
# Uncomment this and comment out aws_instance above to use Spot

# resource "aws_spot_instance_request" "sandbox_host" {
#   count                = var.ec2_use_spot ? 1 : 0
#   ami                  = local.ami_id
#   instance_type        = var.ec2_instance_type
#   key_name             = var.ec2_key_pair_name != "" ? var.ec2_key_pair_name : null
#   iam_instance_profile = aws_iam_instance_profile.sandbox_host.name
#   vpc_security_group_ids = [aws_security_group.sandbox_host.id]
#   spot_price           = var.ec2_spot_max_price != "" ? var.ec2_spot_max_price : null
#   wait_for_fulfillment = true
#
#   root_block_device {
#     volume_size = 50
#     volume_type = "gp3"
#     encrypted   = true
#   }
#
#   user_data = file("${path.module}/../scripts/bootstrap-host.sh")
#
#   tags = {
#     Name = "${var.project_name}-host-spot-${var.environment}"
#   }
# }
