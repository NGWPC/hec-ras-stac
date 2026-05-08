terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Consider locking the version here: version = "~> 6.34.0"
    }
    null = {
      source = "hashicorp/null"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = var.api_name
      Environment = var.environment
      Team        = "FIM-C"
      ManagedBy   = "terraform"
    }
  }
}