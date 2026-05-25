# ---------------------------------------------------------------------------
# AWS ECR Repository
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = "osb-app-${var.environment}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ---------------------------------------------------------------------------
# ECS Cluster
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "main" {
  name = "osb-cluster-${var.environment}"
}

# ---------------------------------------------------------------------------
# IAM Roles (ECS Execution & Task Role)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "ecs_execution_role" {
  name = "osb-ecs-execution-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow reading secrets from AWS Secrets Manager
resource "aws_iam_policy" "secrets_read" {
  name        = "osb-secrets-read-${var.environment}"
  description = "Allows reading production app configuration secrets"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_secrets" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = aws_iam_policy.secrets_read.arn
}

resource "aws_iam_role" "ecs_task_role" {
  name = "osb-ecs-task-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

# Task role needs permissions to write to DynamoDB and SQS
resource "aws_iam_policy" "task_permissions" {
  name = "osb-task-permissions-${var.environment}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.resources.arn,
          aws_dynamodb_table.metrics.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = [
          aws_sqs_queue.tasks.arn,
          aws_sqs_queue.tasks_dlq.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task" {
  role       = aws_iam_role.ecs_task_role.name
  policy_arn = aws_iam_policy.task_permissions.arn
}

# ---------------------------------------------------------------------------
# ALB Load Balancer & Target Groups
# ---------------------------------------------------------------------------
resource "aws_lb" "api" {
  name               = "osb-alb-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "api" {
  name        = "osb-tg-api-${var.environment}"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ---------------------------------------------------------------------------
# ECS Tasks Definitions (API & Worker with OPA sidecar)
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "osb-api-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "broker-api"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
        }
      ]
      environment = [
        { name = "ENTRYPOINT_MODE", value = "api" },
        { name = "PRODUCTION_MODE", value = "true" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "DYNAMODB_TABLE_NAME", value = aws_dynamodb_table.resources.name },
        { name = "DYNAMODB_METRICS_TABLE_NAME", value = aws_dynamodb_table.metrics.name },
        { name = "SQS_QUEUE_URL", value = aws_sqs_queue.tasks.id },
        { name = "SQS_DLQ_URL", value = aws_sqs_queue.tasks_dlq.id },
        { name = "OPA_ENABLED", value = "true" },
        { name = "OPA_URL", value = "http://127.0.0.1:8181" }
      ]
    },
    {
      name      = "opa-sidecar"
      image     = "openpolicyagent/opa:latest"
      essential = true
      portMappings = [
        {
          containerPort = 8181
          hostPort      = 8181
        }
      ]
      command = ["run", "--server", "--addr", ":8181"]
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "osb-worker-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "broker-worker"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true
      environment = [
        { name = "ENTRYPOINT_MODE", value = "worker" },
        { name = "PRODUCTION_MODE", value = "true" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "DYNAMODB_TABLE_NAME", value = aws_dynamodb_table.resources.name },
        { name = "DYNAMODB_METRICS_TABLE_NAME", value = aws_dynamodb_table.metrics.name },
        { name = "SQS_QUEUE_URL", value = aws_sqs_queue.tasks.id },
        { name = "SQS_DLQ_URL", value = aws_sqs_queue.tasks_dlq.id }
      ]
    }
  ])
}

# ---------------------------------------------------------------------------
# ECS Service Deployments
# ---------------------------------------------------------------------------
resource "aws_ecs_service" "api" {
  name            = "osb-service-api-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.api.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "broker-api"
    container_port   = 8000
  }
}

resource "aws_ecs_service" "worker" {
  name            = "osb-service-worker-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.worker.id]
  }
}
