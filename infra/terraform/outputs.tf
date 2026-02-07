output "s3_bucket_name" {
  description = "Name of the S3 storage bucket"
  value       = aws_s3_bucket.sandbox_storage.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 storage bucket"
  value       = aws_s3_bucket.sandbox_storage.arn
}

output "ec2_instance_id" {
  description = "ID of the sandbox host EC2 instance"
  value       = aws_instance.sandbox_host.id
}

output "ec2_public_ip" {
  description = "Public IP of the sandbox host"
  value       = aws_instance.sandbox_host.public_ip
}

output "ec2_public_dns" {
  description = "Public DNS of the sandbox host"
  value       = aws_instance.sandbox_host.public_dns
}

output "iam_role_arn" {
  description = "ARN of the sandbox host IAM role"
  value       = aws_iam_role.sandbox_host.arn
}

output "security_group_id" {
  description = "ID of the sandbox host security group"
  value       = aws_security_group.sandbox_host.id
}
