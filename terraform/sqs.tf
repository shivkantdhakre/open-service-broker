# ---------------------------------------------------------------------------
# SQS Tasks Dead Letter Queue (DLQ)
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "tasks_dlq" {
  name                      = "broker-tasks-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days (max retention)

  tags = {
    Name        = "osb-queue-tasks-dlq-${var.environment}"
    Environment = var.environment
    Project     = "open-service-broker"
  }
}

# ---------------------------------------------------------------------------
# SQS Tasks Primary Queue
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "tasks" {
  name                      = "broker-tasks-${var.environment}"
  message_retention_seconds = 345600 # 4 days
  visibility_timeout_seconds = 120     # Match visibility timeout settings

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tasks_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Name        = "osb-queue-tasks-${var.environment}"
    Environment = var.environment
    Project     = "open-service-broker"
  }
}
