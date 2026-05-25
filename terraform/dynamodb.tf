# ---------------------------------------------------------------------------
# DynamoDB — Broker Resources Table
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "resources" {
  name         = "broker-resources-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "resource_id"
  range_key    = "resource_type"

  attribute {
    name = "resource_id"
    type = "S"
  }

  attribute {
    name = "resource_type"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = "osb-db-resources-${var.environment}"
    Environment = var.environment
    Project     = "open-service-broker"
  }
}

# ---------------------------------------------------------------------------
# DynamoDB — Broker Metrics Table
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "metrics" {
  name         = "broker-metrics-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_name"
  range_key    = "timestamp"

  attribute {
    name = "service_name"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = "osb-db-metrics-${var.environment}"
    Environment = var.environment
    Project     = "open-service-broker"
  }
}
