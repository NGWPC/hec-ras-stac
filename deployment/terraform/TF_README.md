# HEC-RAS STAC Terraform Deployment

This directory contains the Infrastructure as Code (IaC) required to deploy the HEC-RAS STAC API and Browser. The configuration supports two deployment patterns: a functional standalone Docker-based deployment for standalone simplified deployments or testing and a reference architecture meant to support an auto-scaling Enterprise deployment using AWS managed services. The Enterprise pattern is meant to be flushed out only with specific details around the target environment, and is meant to reduce the work for any future Enterprise integrations.

## Prerequisites

- Terraform 1.0 or higher
- AWS CLI configured with appropriate permissions
- An existing VPC and private subnets
- A Route 53 Hosted Zone for DNS records

## Configuration

To deploy the infrastructure, you must create a `.tfvars` file to define your environment-specific settings. It is also suggested that a `backend.tf` be created to properly maintain an encrypted state in an S3 bucket or shared location.  This is generally very specific to the preferences and policies of the deploying team or administrator. 

Below are the primary variables required for a successful deployment.

## Example Configurations (non-functional examples requiring value replacements)
### terraform.tfvars
The following example demonstrates a typical configuration for a standalone deployment. Replace these placeholder values with your own environment-specific data.

```hcl
# Core Environment Settings
environment        = "test"
aws_region         = "us-east-1"
api_name           = "hec-ras-stac"
hosted_zone_id     = "Z00000000000000000000"
# Policy ARN for Session Manager access
session_manager_logging_policy_arn = "arn:aws:iam::123456789012:policy/YourSessionManagerPolicy"

# Network & Compute
vpc_name             = "Your-VPC-Name"
subnet_name_pattern  = "Your-Private-Subnet-Pattern*"
instance_type        = "t3.xlarge"
root_volume_size     = 100
ubuntu_version       = "jammy-22.04"
architecture         = "amd64"
additional_vpc_cidrs = ["10.0.0.0/16"]

# Deployment Strategy (Standalone Mode)
enterprise_mode = false

# Application Versions
api_image_version     = "4.0.3"
browser_image_version = "3.3.4"

# S3 Access Configuration
# Define buckets the application should read from and where to store backups
s3_read_paths = [
  "your-stac-bucket",   # e.g., hv-fim-dev-stac
  "your-data-bucket",   # e.g., hv-fim-dev-data
]
s3_write_paths = ["your-data-bucket/your-data-prefix/backups/*"]
backup_s3_uri  = "s3://your-data-bucket/your-data-prefix/backups/stac-db/"

# Logging
log_retention_days = 7

# key_name = "your-aws-key-pair-name"  # Optional: required for SSH access
```

### backend.tf
```hcl
terraform {
  backend "s3" {
    bucket         = "your-infra-state-bucket"
    key            = "terraform/hec-ras-stac/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
}
```

### Core Settings
- `environment`: The name of the environment (e.g., "dev", "prod").
- `aws_region`: The AWS region where resources will be deployed.
- `api_name`: A unique identifier for the application used in resource naming and tagging.
- `hosted_zone_id`: The ID of the Route 53 Hosted Zone where DNS records will be created.

### Network and Compute
- `vpc_name`: The name tag of the VPC to deploy into.
- `subnet_name_pattern`: A string pattern used to identify the private subnets for deployment.
- `instance_type`: The EC2 instance type (e.g., "t3.xlarge").
- `ubuntu_version`: The Ubuntu release version (default: "jammy-22.04").
- `architecture`: The CPU architecture, either "amd64" or "arm64".
- `key_name`: (Optional) The name of an existing EC2 key pair. Required if you need SSH access to the instance.

### S3 Access and Backups
- `s3_read_paths`: List of S3 buckets the application reads from. Expects two buckets: the STAC metadata bucket (e.g., `hv-fim-dev-stac`) and the data bucket (e.g., `hv-fim-dev-data`).
- `s3_write_paths`: Bucket path where the application has write access. Scoped to the backups prefix within the data prefix on the data bucket (e.g., `your-data-bucket/your-data-prefix/backups/*`).
- `backup_s3_uri`: Full S3 URI for pgSTAC database backups. Nested under the data prefix in the data bucket (e.g., `s3://your-data-bucket/your-data-prefix/backups/stac-db/`). Leave empty for Enterprise mode (external RDS manages its own backups).

### Deployment Mode
- `enterprise_mode`: Set to `false` for a standalone single-instance deployment or `true` for a load-balanced, auto-scaling deployment.

## Deployment Steps

1. Initialize Terraform:
   ```bash
   terraform init
2. Create a custom variable file (Example: my-deploy.tfvars) and populate it with your environment details based on the variables described above.
3. Generate and review the execution plan:
    ```bash
    terraform plan -var-file="my-deploy.tfvars"
4. Apply the configuration:
    ```bash
    terraform apply -var-file="my-deploy.tfvars"
## Infrastructure Details
### Standalone Mode

In standalone mode, the STAC API, STAC Browser, and pgSTAC database run as Docker containers on a single EC2 instance. Terraform automatically generates an IAM Instance Profile restricted to the S3 paths provided in your variables.

### Automated Backups
For standalone deployments, a backup script is automatically installed and configured as a weekly cron job. If backup_s3_uri is provided, backups will be synced to S3; otherwise, they remain stored locally on the instance for seven days.

### Health Monitoring
A health check script is provided at /opt/hec-ras-stac/deployment/health-check.sh on the deployed instance to verify the status of Docker containers, API responsiveness, and S3 connectivity.

## Outputs
Upon successful application, Terraform will provide several outputs:

- stac_api_url: The fully qualified URL for the STAC API.
- stac_browser_url: The fully qualified URL for the STAC Browser.
- standalone_instance_ip: The private IP of the instance for internal access.
- ssh_instructions: The SSH command to connect to the instance (requires `key_name` to be set).
