# ─── IAM Role for EC2 Sandbox Host ────────────────────────────────────────────
# This role is assumed by the EC2 instance. Containers inherit these permissions
# via the instance metadata service.

resource "aws_iam_role" "sandbox_host" {
  name = "${var.project_name}-host-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

# S3 access scoped to the sandbox storage bucket
resource "aws_iam_role_policy" "sandbox_s3_access" {
  name = "${var.project_name}-s3-access"
  role = aws_iam_role.sandbox_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ListBucket"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = aws_s3_bucket.sandbox_storage.arn
      },
      {
        Sid    = "ReadWriteObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload",
        ]
        Resource = "${aws_s3_bucket.sandbox_storage.arn}/*"
      }
    ]
  })
}

# CloudWatch Logs for sandbox monitoring
resource "aws_iam_role_policy" "sandbox_cloudwatch" {
  name = "${var.project_name}-cloudwatch"
  role = aws_iam_role.sandbox_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/matrx/sandbox/*"
      }
    ]
  })
}

# ECR pull (for pulling sandbox Docker images from ECR)
resource "aws_iam_role_policy" "sandbox_ecr_pull" {
  name = "${var.project_name}-ecr-pull"
  role = aws_iam_role.sandbox_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "sandbox_host" {
  name = "${var.project_name}-host-${var.environment}"
  role = aws_iam_role.sandbox_host.name
}
