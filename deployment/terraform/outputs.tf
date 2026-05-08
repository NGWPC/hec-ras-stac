# ==========================================
# Application URLs
# ==========================================

output "stac_api_url" {
  description = "The URL to access the STAC API"
  # Standalone uses HTTP with custom port; Enterprise uses ALB with HTTPS
  value = local.is_standalone ? "http://${local.full_domain_name}:8082" : "https://${local.full_domain_name}"
}

output "stac_browser_url" {
  description = "The URL to access the STAC Browser UI"
  # Note: Enterprise routing for the browser UI is pending full implementation.
  value = local.is_standalone ? "http://${local.full_domain_name}:8080" : "https://${local.full_domain_name}/browser (Pending Enterprise Implementation)"
}

# ==========================================
# Infrastructure Endpoints
# ==========================================

output "standalone_instance_ip" {
  description = "The private IP of the standalone EC2 instance (if deployed)"
  value       = local.is_standalone ? aws_instance.standalone_instance[0].private_ip : null
}

output "load_balancer_dns" {
  description = "The raw DNS name of the Application Load Balancer (if in enterprise mode)"
  value       = !local.is_standalone ? aws_lb.app[0].dns_name : null
}

# ==========================================
# Helpful Commands
# ==========================================

output "ssh_instructions" {
  description = "Helpful command for connecting to the standalone instance"
  value       = local.is_standalone ? "ssh -i /path/to/key.pem ubuntu@${aws_instance.standalone_instance[0].private_ip}" : "Use Session Manager to connect to the ASG instances."
}