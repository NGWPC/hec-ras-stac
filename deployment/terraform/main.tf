locals {
  common_tags = {
    Application = var.api_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
  is_standalone    = !var.enterprise_mode
  full_domain_name = "${var.api_name}.${trimsuffix(data.aws_route53_zone.selected.name, ".")}"
}

# Security Groups
resource "aws_security_group" "instance" {
  name_prefix = "${var.api_name}-${var.environment}-instance"
  description = "Security group for API instances"
  vpc_id      = data.aws_vpc.main.id

  dynamic "ingress" {
    for_each = var.app_ports
    content {
      from_port = ingress.value
      to_port   = ingress.value
      protocol  = "tcp"
      # Allows traffic from the ALB SG only if Enterprise Mode is enabled
      security_groups = var.enterprise_mode ? aws_security_group.alb[*].id : null
      # Always allow internal VPC traffic for internal communication
      cidr_blocks = concat([data.aws_vpc.main.cidr_block], var.additional_vpc_cidrs)
    }
  }

  # Keep SSH explicitly separated for VPC/VPN access only
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = concat([data.aws_vpc.main.cidr_block], var.additional_vpc_cidrs)
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.api_name}-${var.environment}-instance"
  })

  lifecycle {
    create_before_destroy = true
  }
}


# The ALB security group should now only allow internal access
resource "aws_security_group" "alb" {
  count = local.is_standalone ? 0 : 1

  name_prefix = "${var.api_name}-${var.environment}-alb"
  description = "Security group for API load balancer"
  vpc_id      = data.aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = concat([data.aws_vpc.main.cidr_block], var.additional_vpc_cidrs)
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = concat([data.aws_vpc.main.cidr_block], var.additional_vpc_cidrs)
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${var.api_name}-${var.environment}-alb"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# IAM Resources
resource "aws_iam_role" "instance_role" {
  name = "${var.api_name}-${var.environment}-instance-role"

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

  lifecycle {
    create_before_destroy = true
  }

  tags = local.common_tags
}

resource "aws_iam_role_policy" "instance_policy" {
  name_prefix = "instance-policy"
  role        = aws_iam_role.instance_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = flatten([
      # Conditionally include Secrets Manager access
      var.enterprise_mode ? [{
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = compact([
          var.secrets_manager_arn,
          var.ad_secret
        ])
      }] : [],

      [{
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          "${aws_cloudwatch_log_group.api_logs.arn}:*",
          aws_cloudwatch_log_group.api_logs.arn
        ]
      }],

      # Conditionally include S3 List access (if any paths exist)
      length(concat(var.s3_read_paths, var.s3_write_paths)) > 0 ? [{
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = distinct(flatten([
          for path in concat(var.s3_read_paths, var.s3_write_paths) :
          "arn:aws:s3:::${split("/", path)[0]}"
        ]))
      }] : [],

      # Conditionally include S3 Read access
      length(var.s3_read_paths) > 0 ? [{
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectAcl"
        ]
        Resource = [
          for path in var.s3_read_paths :
          can(regex("/\\*$", path)) ? "arn:aws:s3:::${path}" : "arn:aws:s3:::${path}/*"
        ]
      }] : [],

      # Conditionally include S3 Write access
      length(var.s3_write_paths) > 0 ? [{
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectAcl",
          "s3:PutObject",
          "s3:PutObjectAcl",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts"
        ]
        Resource = [
          for path in var.s3_write_paths :
          can(regex("/\\*$", path)) ? "arn:aws:s3:::${path}" : "arn:aws:s3:::${path}/*"
        ]
      }] : []
    ])
  })
}

resource "aws_iam_instance_profile" "instance_profile" {
  name = "${var.api_name}-${var.environment}-instance-profile"
  role = aws_iam_role.instance_role.name
  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "session_manager_logging" {
  role       = aws_iam_role.instance_role.id
  policy_arn = var.session_manager_logging_policy_arn
}

# Standalone Deployment Resources
resource "aws_instance" "standalone_instance" {
  count = local.is_standalone ? 1 : 0

  ami           = coalesce(var.ami_id, data.aws_ami.ubuntu.id)
  instance_type = var.instance_type
  key_name      = var.key_name

  root_block_device {
    volume_type = var.root_volume_type
    volume_size = var.root_volume_size
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  iam_instance_profile        = aws_iam_instance_profile.instance_profile.name
  vpc_security_group_ids      = [aws_security_group.instance.id]
  subnet_id                   = data.aws_subnets.private.ids[0]
  associate_public_ip_address = false

  user_data_replace_on_change = true
  user_data_base64 = base64gzip(
    templatefile(
      var.enterprise_mode ? "${path.module}/templates/user_data_enterprise.sh.tpl" : "${path.module}/templates/user_data_standalone.sh.tpl",
      {
        aws_region            = var.aws_region
        db_host               = var.db_host
        db_port               = var.db_port
        db_name               = var.db_name
        secrets_manager_arn   = var.secrets_manager_arn
        directory_id          = var.directory_id
        directory_name        = var.directory_name
        ad_secret             = var.ad_secret
        ad_dns_1              = var.enterprise_mode && length(var.ad_dns_servers) > 0 ? var.ad_dns_servers[0] : ""
        ad_dns_2              = var.enterprise_mode && length(var.ad_dns_servers) > 1 ? var.ad_dns_servers[1] : ""
        log_group_name        = aws_cloudwatch_log_group.api_logs.name
        environment           = var.environment
        enterprise_mode       = var.enterprise_mode
        alb_target_port       = var.alb_target_port
        s3_read_paths         = join(",", var.s3_read_paths)
        s3_write_paths        = join(",", var.s3_write_paths)
        backup_s3_uri         = var.backup_s3_uri
        stac_catalog_path     = var.stac_catalog_path
        api_image_version     = var.api_image_version
        browser_image_version = var.browser_image_version
        domain_name           = local.full_domain_name
      }
    )
  )

  tags = merge(local.common_tags, {
    Name = "${var.api_name}-${var.environment}"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# Enterprise Environment Resources
resource "aws_launch_template" "app" {
  count = local.is_standalone ? 0 : 1

  name_prefix            = "${var.api_name}-${var.environment}"
  image_id               = coalesce(var.ami_id, data.aws_ami.ubuntu.id)
  instance_type          = var.instance_type
  update_default_version = true

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.instance.id]
    delete_on_termination       = true
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.root_volume_size
      volume_type           = var.root_volume_type
      encrypted             = true
      kms_key_id            = var.kms_key_arn
      delete_on_termination = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.instance_profile.name
  }

  user_data = base64gzip(
    templatefile(
      var.enterprise_mode ? "${path.module}/templates/user_data_enterprise.sh.tpl" : "${path.module}/templates/user_data_standalone.sh.tpl",
      {
        aws_region            = var.aws_region
        db_host               = var.db_host
        db_port               = var.db_port
        db_name               = var.db_name
        secrets_manager_arn   = var.secrets_manager_arn
        directory_id          = var.directory_id
        directory_name        = var.directory_name
        ad_secret             = var.ad_secret
        ad_dns_1              = var.enterprise_mode && length(var.ad_dns_servers) > 0 ? var.ad_dns_servers[0] : ""
        ad_dns_2              = var.enterprise_mode && length(var.ad_dns_servers) > 1 ? var.ad_dns_servers[1] : ""
        log_group_name        = aws_cloudwatch_log_group.api_logs.name
        environment           = var.environment
        enterprise_mode       = var.enterprise_mode
        alb_target_port       = var.alb_target_port
        s3_read_paths         = join(",", var.s3_read_paths)
        s3_write_paths        = join(",", var.s3_write_paths)
        stac_catalog_path     = var.stac_catalog_path
        api_image_version     = var.api_image_version
        browser_image_version = var.browser_image_version
        domain_name           = local.full_domain_name
      }
    )
  )

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name = "${var.api_name}-${var.environment}"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = local.common_tags
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "app" {
  count = local.is_standalone ? 0 : 1

  name                      = "${var.api_name}-${var.environment}"
  desired_capacity          = var.asg_desired_capacity
  max_size                  = var.asg_max_size
  min_size                  = var.asg_min_size
  target_group_arns         = [aws_lb_target_group.app[0].arn]
  vpc_zone_identifier       = data.aws_subnets.private.ids
  health_check_grace_period = 900
  health_check_type         = "ELB"

  launch_template {
    id      = aws_launch_template.app[0].id
    version = "$Latest"
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 100
      instance_warmup        = 900
      checkpoint_delay       = 900
      checkpoint_percentages = [25, 50, 75, 100]
    }
  }

  dynamic "tag" {
    for_each = merge(local.common_tags, {
      Name = "${var.api_name}-${var.environment}"
    })
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }

  depends_on = [aws_lb.app]
}

# Load Balancer Resources
resource "aws_lb" "app" {
  count = local.is_standalone ? 0 : 1

  name                       = "${var.api_name}-${var.environment}"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb[0].id]
  subnets                    = data.aws_subnets.private.ids
  idle_timeout               = 600
  enable_deletion_protection = var.enable_deletion_protection

  access_logs {
    bucket  = aws_s3_bucket.alb_logs[0].id
    prefix  = "${var.api_name}-${var.environment}"
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.api_name}-${var.environment}"
  })
}

resource "aws_lb_target_group" "app" {
  count = local.is_standalone ? 0 : 1

  name     = "${var.api_name}-${var.environment}"
  port     = var.alb_target_port
  protocol = "HTTP"
  vpc_id   = data.aws_vpc.main.id

  health_check {
    enabled             = true
    healthy_threshold   = 3
    interval            = 30
    matcher             = "200"
    path                = var.health_check_path
    port                = "traffic-port"
    timeout             = 10
    unhealthy_threshold = 3
  }

  tags = merge(local.common_tags, {
    Name = "${var.api_name}-${var.environment}"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "https" {
  count = local.is_standalone || var.certificate_arn == null ? 0 : 1

  load_balancer_arn = aws_lb.app[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS-1-2-2017-01"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app[0].arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  count = local.is_standalone ? 0 : 1

  load_balancer_arn = aws_lb.app[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ALB Logs Bucket
resource "aws_s3_bucket" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = "${var.api_name}-${var.environment}-alb-logs-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = false
  }

  tags = local.common_tags
}

resource "aws_s3_bucket_versioning" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = aws_s3_bucket.alb_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = aws_s3_bucket.alb_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = aws_s3_bucket.alb_logs[0].id

  rule {
    id     = "cleanup_old_logs"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = aws_s3_bucket.alb_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${lookup(var.alb_service_account_ids, var.aws_region)}:root"
        }
        Action = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.alb_logs[0].arn}/*",
        ]
      },
      {
        Effect = "Allow"
        Principal = {
          Service = "delivery.logs.amazonaws.com"
        }
        Action = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.alb_logs[0].arn}/*",
        ]
        Condition = {
          StringEquals = {
            "s3:x-amz-acl" : "bucket-owner-full-control"
          }
        }
      },
      {
        Effect = "Allow"
        Principal = {
          Service = "delivery.logs.amazonaws.com"
        }
        Action   = "s3:GetBucketAcl"
        Resource = aws_s3_bucket.alb_logs[0].arn
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.alb_logs[0].arn,
          "${aws_s3_bucket.alb_logs[0].arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" : "false"
          }
        }
      }
    ]
  })
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  count  = local.is_standalone ? 0 : 1
  bucket = aws_s3_bucket.alb_logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Route 53 Records
resource "aws_route53_record" "standalone" {
  count = local.is_standalone ? 1 : 0

  zone_id = var.hosted_zone_id
  name    = local.full_domain_name
  type    = "A"
  ttl     = 300

  records = [
    aws_instance.standalone_instance[0].private_ip
  ]
}

resource "aws_route53_record" "app" {
  count = local.is_standalone ? 0 : 1

  zone_id = var.hosted_zone_id
  name    = local.full_domain_name
  type    = "A"

  alias {
    name                   = aws_lb.app[0].dns_name
    zone_id                = aws_lb.app[0].zone_id
    evaluate_target_health = true
  }
}

# CloudWatch Resources
resource "aws_cloudwatch_log_group" "api_logs" {
  name              = "/aws/ec2/${var.api_name}-${var.environment}"
  retention_in_days = var.log_retention_days

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "high_cpu" {
  count = local.is_standalone || var.sns_alert_topic_arn == null ? 0 : 1

  alarm_name          = "${var.api_name}-${var.environment}-high-cpu"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "High CPU utilization for ${var.api_name} in ${var.environment}"
  alarm_actions       = [var.sns_alert_topic_arn]
  ok_actions          = [var.sns_alert_topic_arn]

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.app[0].name
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "high_memory" {
  count = local.is_standalone || var.sns_alert_topic_arn == null ? 0 : 1

  alarm_name          = "${var.api_name}-${var.environment}-high-memory"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "MemoryUtilization"
  namespace           = "System/Linux"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "High memory utilization for ${var.api_name} in ${var.environment}"
  alarm_actions       = [var.sns_alert_topic_arn]
  ok_actions          = [var.sns_alert_topic_arn]

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.app[0].name
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "high_5xx_errors" {
  count = local.is_standalone || var.sns_alert_topic_arn == null ? 0 : 1

  alarm_name          = "${var.api_name}-${var.environment}-high-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "High 5XX error count for ${var.api_name} in ${var.environment}"
  alarm_actions       = [var.sns_alert_topic_arn]
  ok_actions          = [var.sns_alert_topic_arn]

  dimensions = {
    LoadBalancer = aws_lb.app[0].arn_suffix
  }

  tags = local.common_tags
}

resource "null_resource" "asg_refresh" {
  count      = local.is_standalone ? 0 : 1
  depends_on = [aws_autoscaling_group.app]

  triggers = {
    # Trigger ASG instance refresh when user_data content changes
    user_data_hash = base64sha256(templatefile(
      var.enterprise_mode ? "${path.module}/templates/user_data_enterprise.sh.tpl" : "${path.module}/templates/user_data_standalone.sh.tpl",
      {
        aws_region            = var.aws_region
        db_host               = var.db_host
        db_port               = var.db_port
        db_name               = var.db_name
        secrets_manager_arn   = var.secrets_manager_arn
        directory_id          = var.directory_id
        directory_name        = var.directory_name
        ad_secret             = var.ad_secret
        ad_dns_1              = var.enterprise_mode && length(var.ad_dns_servers) > 0 ? var.ad_dns_servers[0] : ""
        ad_dns_2              = var.enterprise_mode && length(var.ad_dns_servers) > 1 ? var.ad_dns_servers[1] : ""
        log_group_name        = aws_cloudwatch_log_group.api_logs.name
        environment           = var.environment
        enterprise_mode       = var.enterprise_mode
        alb_target_port       = var.alb_target_port
        s3_read_paths         = join(",", var.s3_read_paths)
        s3_write_paths        = join(",", var.s3_write_paths)
        stac_catalog_path     = var.stac_catalog_path
        api_image_version     = var.api_image_version
        browser_image_version = var.browser_image_version
      }
    ))
  }

  provisioner "local-exec" {
    command = <<EOF
      aws autoscaling start-instance-refresh \
        --auto-scaling-group-name "${var.api_name}-${var.environment}" \
        --preferences '{"MinHealthyPercentage": 100, "InstanceWarmup": 900, "CheckpointDelay": 900, "CheckpointPercentages": [25, 50, 75, 100]}' \
        --desired-configuration '{"LaunchTemplate": {"LaunchTemplateId": "${aws_launch_template.app[0].id}", "Version": "${aws_launch_template.app[0].latest_version}"}}'
    EOF
  }
}