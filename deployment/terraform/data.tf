# ==========================================
# Account & Network Data
# ==========================================

data "aws_caller_identity" "current" {}

# ALB Service Account IDs for S3 Bucket Policy Permissions
variable "alb_service_account_ids" {
  description = "Managed AWS Account IDs for ALB access logging to S3"
  type        = map(string)
  default = {
    "us-east-1"     = "127311923021"
    "us-east-2"     = "033677994240"
    "us-west-1"     = "027434742980"
    "us-west-2"     = "797873946194"
    "us-gov-east-1" = "190560391635"
    "us-gov-west-1" = "048591011584"
  }
}

data "aws_vpc" "main" {
  tags = {
    Name = var.vpc_name
  }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }

  filter {
    name   = "tag:Name"
    values = [var.subnet_name_pattern]
  }
}

# ==========================================
# Route 53 Data
# ==========================================
data "aws_route53_zone" "selected" {
  zone_id = var.hosted_zone_id
}

# ==========================================
# Ubuntu AMIs
# ==========================================
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name = "name"
    # Dynamically matches: ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-* # Or: ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*
    values = ["ubuntu/images/hvm-ssd*/ubuntu-${var.ubuntu_version}-${var.architecture}-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name = "architecture"
    # AWS uses "x86_64" in the architecture filter, but Canonical uses "amd64" in the AMI name
    values = [var.architecture == "amd64" ? "x86_64" : var.architecture]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}
