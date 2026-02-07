# ─── S3 Bucket for Sandbox Storage ────────────────────────────────────────────

resource "aws_s3_bucket" "sandbox_storage" {
  bucket = var.s3_bucket_name

  tags = {
    Name = "${var.project_name}-storage"
  }
}

resource "aws_s3_bucket_versioning" "sandbox_storage" {
  bucket = aws_s3_bucket.sandbox_storage.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sandbox_storage" {
  bucket = aws_s3_bucket.sandbox_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "sandbox_storage" {
  bucket = aws_s3_bucket.sandbox_storage.id

  # Hot storage: keep versions for 7 days for recovery
  rule {
    id     = "hot-storage-lifecycle"
    status = "Enabled"

    filter {
      prefix = "users/"
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }

  # Clean up incomplete multipart uploads
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_public_access_block" "sandbox_storage" {
  bucket = aws_s3_bucket.sandbox_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
