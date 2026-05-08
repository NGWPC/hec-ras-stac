variable "environment" {
  description = "Environment name (test or oe)"
  type        = string

  validation {
    condition     = contains(["test", "oe"], var.environment)
    error_message = "Environment must be either 'test' or 'oe'."
  }
}

variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
}

variable "api_name" {
  description = "Name of the API application"
  type        = string
}

variable "vpc_name" {
  description = "Name of the VPC to deploy into"
  type        = string
  default     = "main"
}

variable "subnet_name_pattern" {
  description = "Pattern to match for target subnets in the VPC"
  type        = string
  default     = "App*"
}

variable "hosted_zone_id" {
  description = "Route53 hosted zone ID for DNS records"
  type        = string
}

variable "ami_id" {
  description = "AMI ID for EC2 instances. If not provided, latest specified ubuntu AMI will be used."
  type        = string
  default     = null

  validation {
    condition     = var.ami_id == null || can(regex("^ami-[a-f0-9]{17}$", var.ami_id))
    error_message = "If provided, AMI ID must be valid (e.g., ami-123456789abcdef01)."
  }
}

variable "ubuntu_version" {
  description = "Ubuntu release version to use if ami_id is not provided."
  type        = string
  default     = "noble-24.04" # Options: "jammy-22.04" or "noble-24.04"
}

variable "architecture" {
  description = "CPU architecture for the AMI (amd64 or arm64)"
  type        = string
  default     = "amd64"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
}

variable "root_volume_type" {
  description = "Type of root volume (gp2, gp3, io1, etc.)"
  type        = string
  default     = "gp3"
}

variable "root_volume_size" {
  description = "Size of root volume in GB (Suggested: 100 to 200)"
  type        = number
  default     = 100
}

variable "key_name" {
  description = "The name of the AWS Key Pair to use for instance authentication"
  type        = string
  default     = null
}

variable "asg_min_size" {
  description = "Minimum number of instances in the ASG"
  type        = number
  default     = 1
}

variable "asg_max_size" {
  description = "Maximum number of instances in the ASG"
  type        = number
  default     = 3
}

variable "asg_desired_capacity" {
  description = "Desired number of instances in the ASG"
  type        = number
  default     = 2
}

variable "certificate_arn" {
  description = "ARN of ACM certificate for HTTPS. Required only for non-test, load balanced environments."
  type        = string
  default     = null

  validation {
    condition     = var.certificate_arn == null || can(regex("^arn:aws:acm:[a-z0-9-]+:\\d{12}:certificate/[a-zA-Z0-9-]+$", var.certificate_arn))
    error_message = "If provided, must be a valid ACM certificate ARN."
  }
}

variable "health_check_path" {
  description = "Path for ALB health check"
  type        = string
  default     = "/health"
}

variable "health_check_interval" {
  description = "Interval for health checks (in seconds)"
  type        = number
  default     = 15
}

variable "health_check_timeout" {
  description = "Timeout for health checks (in seconds)"
  type        = number
  default     = 5
}

variable "health_check_healthy_threshold" {
  description = "Number of consecutive successful health checks before considering target healthy"
  type        = number
  default     = 2
}

variable "health_check_unhealthy_threshold" {
  description = "Number of consecutive failed health checks before considering target unhealthy"
  type        = number
  default     = 2
}

variable "log_retention_days" {
  description = "Number of days to retain CloudWatch logs"
  type        = number
  default     = 30
}

variable "sns_alert_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarms. Required only for non-test environments."
  type        = string
  default     = null

  validation {
    condition     = var.sns_alert_topic_arn == null || can(regex("^arn:aws:sns:[a-z0-9-]+:\\d{12}:[a-zA-Z0-9-_]+$", var.sns_alert_topic_arn))
    error_message = "If provided, must be a valid SNS topic ARN."
  }
}

variable "enable_deletion_protection" {
  description = "Enable deletion protection for ALB"
  type        = bool
  default     = true
}

variable "kms_key_arn" {
  description = "ARN of KMS key for encryption. If not provided, AWS managed keys will be used."
  type        = string
  default     = null
}

variable "session_manager_logging_policy_arn" {
  description = "ARN of the Session Manager logging policy"
  type        = string
  default     = null
}

variable "additional_vpc_cidrs" {
  description = "List of additional VPC CIDR blocks that should have access to the instance in test environment"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.additional_vpc_cidrs : can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$", cidr))])
    error_message = "All CIDR blocks must be valid IPv4 CIDR notation (e.g., '10.0.0.0/16')."
  }
}

# ==========================================
# Multi-Service / Docker Variables
# ==========================================

variable "app_ports" {
  description = "List of application ports to open on the EC2 instance Security Group (e.g., [8080, 8082])"
  type        = list(number)
  default     = [8080, 8082, 8083]
}

variable "alb_target_port" {
  description = "The primary port the ALB will forward traffic to"
  type        = number
  default     = 8082
}

variable "api_image_version" {
  description = "Docker image tag for the STAC FastAPI container"
  type        = string
  default     = "4.0.3"
}

variable "browser_image_version" {
  description = "Docker image tag for the STAC Browser container"
  type        = string
  default     = "3.3.4"
}

# ==========================================
# S3 Access Configuration
# ==========================================

variable "s3_read_paths" {
  description = "List of S3 buckets or prefixes for read-only access (e.g., ['my-bucket', 'my-bucket/path/*'])"
  type        = list(string)
  default     = []
}

variable "s3_write_paths" {
  description = "List of S3 buckets or prefixes for read/write access"
  type        = list(string)
  default     = []
}

variable "backup_s3_uri" {
  description = "The specific S3 URI where standalone database backups should be stored (s3://my-bucket/backups/stac-db/). Leave blank for Enterprise mode."
  type        = string
  default     = ""
}

variable "stac_catalog_path" {
  description = "S3 key prefix where the STAC catalog lives within the STAC bucket (e.g., hec-ras-stac/)"
  type        = string
  default     = "hec-ras-stac/"
}

# ==========================================
# Deployment Strategy Pattern
# ==========================================

variable "enterprise_mode" {
  description = "If true, deploy with Active Directory join, external RDS, and autoscaling. If false, deploy the standalone local docker configuration."
  type        = bool
  default     = true
}

# ==========================================
# Enterprise Variables (Optional for Standalone)
# ==========================================

variable "secrets_manager_arn" {
  description = "ARN of the Secrets Manager secret containing RDS credentials. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "db_host" {
  description = "Database host endpoint. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "db_name" {
  description = "Database name. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "db_port" {
  description = "Database port"
  type        = number
  default     = 5432
}

variable "directory_id" {
  description = "ID of the AWS Managed Microsoft AD directory for Windows instances. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "directory_name" {
  description = "Fully qualified domain name of the AWS Managed Microsoft AD. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "ad_secret" {
  description = "ARN of the Secrets Manager secret containing AD join credentials. Optional if enterprise_mode is false."
  type        = string
  default     = ""
}

variable "ad_dns_servers" {
  description = "List of IP addresses for the AD DNS servers. Only used if enterprise_mode is true."
  type        = list(string)
  default     = [] # Default to empty for standalone mode
}
