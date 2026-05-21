# CloudFront distribution sitting in front of the ALB.
#
# Provides HTTPS at the edge using the auto-issued cert on the default
# *.cloudfront.net domain — no domain registration required. CloudFront
# talks HTTP to the ALB (which has no cert), but the viewer→edge leg is
# always TLS.
#
# Cost: free tier covers 1 TB outbound + 10M HTTPS requests per month for
# 12 months. After that, ~$0.085/GB out — for a demo, essentially $0.
#
# Caveat: deployment takes 5~15 minutes (CloudFront has to roll out to
# every edge POP). `terraform apply` will sit on this resource for a while.

variable "alb_dns_name" {
  description = "DNS name of the ALB provisioned by the Load Balancer Controller. Get it with: kubectl get ingress woms -n woms -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'"
  type        = string
  default     = ""
}

# Only provision CloudFront when an ALB hostname is supplied. Lets us run
# `terraform apply` for the cluster *before* the Ingress exists, then a
# second apply once we have the ALB.
locals {
  cloudfront_enabled = var.alb_dns_name != ""
}

resource "aws_cloudfront_distribution" "main" {
  count = local.cloudfront_enabled ? 1 : 0

  enabled         = true
  comment         = "${var.project}-${var.environment} edge"
  http_version    = "http2"
  is_ipv6_enabled = true

  # PriceClass_100 = US/Canada/Europe POPs only — cheapest, plenty for demo.
  # PriceClass_200 adds Asia/Africa/Middle East — slightly faster for users
  # in Taiwan but costs more. _All is the most expensive.
  price_class = "PriceClass_200"

  origin {
    domain_name = var.alb_dns_name
    origin_id   = "woms-alb"

    custom_origin_config {
      origin_protocol_policy = "http-only"
      http_port              = 80
      https_port             = 443
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }

  # All requests pass through to the ALB with no edge caching. Simpler
  # mental model — once we know which paths are safe to cache (`/assets/*`,
  # the React bundle hashes), we can add per-path behaviors.
  default_cache_behavior {
    target_origin_id       = "woms-alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # Managed-CachingDisabled — passes through without caching.
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    # Managed-AllViewer — forwards all cookies / headers / query strings.
    # Important: includes Authorization header so JWT cookies survive.
    origin_request_policy_id = "216adef6-5c7f-47e4-b989-5492eafa07d3"
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}
