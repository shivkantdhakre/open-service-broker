"""
Local Stack Runner — Run the full Open Service Broker stack locally without Docker.

Starts:
1. Moto mock AWS server (DynamoDB + SQS) on port 4566
2. Pre-creates DynamoDB tables and SQS queue + DLQ
3. Mock Sovereign Envoy Control Plane on port 8080
4. Broker Worker daemon
5. Broker API FastAPI server on port 8000

Press Ctrl+C to stop all services cleanly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import json
import boto3

# Force UTF-8 encoding on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

PROCESSES: list[subprocess.Popen[str] | subprocess.Popen[bytes]] = []


def print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(f" [*] {title}")
    print("=" * 80)


def cleanup() -> None:
    print("\nStopping all services...")
    for proc in PROCESSES:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print("All services stopped.")


def main() -> None:
    # Load .env file to set up identical AWS credentials
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

    os.environ["PYTHONPATH"] = os.path.join(os.getcwd(), "src")

    try:
        # 1. Start Moto Server
        print_header("Starting Moto Server (Local SQS & DynamoDB)...")
        moto_proc = subprocess.Popen(
            [sys.executable, "-m", "moto.server", "-p", "4566"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PROCESSES.append(moto_proc)

        # Wait for Moto to be ready
        print("Waiting for Moto server on http://localhost:4566...")
        for _ in range(30):
            try:
                urllib.request.urlopen("http://localhost:4566")
                break
            except urllib.error.URLError:
                time.sleep(0.5)
        else:
            print("Error: Moto server failed to start.")
            sys.exit(1)
        print("Moto server is ready!")

        # 2. Pre-create DynamoDB tables and SQS queues
        print_header("Initializing AWS Mock Resources...")
        aws_endpoint = "http://localhost:4566"
        db_client = boto3.client("dynamodb", region_name="us-east-1", endpoint_url=aws_endpoint)
        sqs_client = boto3.client("sqs", region_name="us-east-1", endpoint_url=aws_endpoint)

        # DynamoDB table: broker-resources
        try:
            db_client.create_table(
                TableName="broker-resources",
                AttributeDefinitions=[
                    {"AttributeName": "resource_id", "AttributeType": "S"},
                    {"AttributeName": "resource_type", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "resource_id", "KeyType": "HASH"},
                    {"AttributeName": "resource_type", "KeyType": "RANGE"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            print("Created DynamoDB table: broker-resources")
        except Exception as e:
            print(f"DynamoDB resources table info: {e}")

        # DynamoDB table: broker-metrics
        try:
            db_client.create_table(
                TableName="broker-metrics",
                AttributeDefinitions=[
                    {"AttributeName": "service_name", "AttributeType": "S"},
                    {"AttributeName": "timestamp", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "service_name", "KeyType": "HASH"},
                    {"AttributeName": "timestamp", "KeyType": "RANGE"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            print("Created DynamoDB table: broker-metrics")
        except Exception as e:
            print(f"DynamoDB metrics table info: {e}")

        # SQS DLQ queue
        try:
            dlq_resp = sqs_client.create_queue(QueueName="broker-tasks-dlq")
            dlq_url = dlq_resp["QueueUrl"]
            attrs = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])
            dlq_arn = attrs["Attributes"]["QueueArn"]
            print(f"Created SQS DLQ: broker-tasks-dlq ({dlq_url})")

            # SQS Tasks queue
            redrive_policy = json.dumps({
                "deadLetterTargetArn": dlq_arn,
                "maxReceiveCount": "3"
            })
            tasks_resp = sqs_client.create_queue(
                QueueName="broker-tasks",
                Attributes={"RedrivePolicy": redrive_policy}
            )
            queue_url = tasks_resp["QueueUrl"]
            print(f"Created SQS Queue: broker-tasks ({queue_url})")

            # Export these queue URLs to subprocess environments
            os.environ["SQS_QUEUE_URL"] = queue_url
            os.environ["SQS_DLQ_URL"] = dlq_url
        except Exception as e:
            print(f"SQS initialization info: {e}")

        # 3. Start Mock Sovereign
        print_header("Starting Mock Sovereign Control Plane...")
        sovereign_proc = subprocess.Popen(
            [sys.executable, "-m", "broker.mock_sovereign"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        PROCESSES.append(sovereign_proc)
        time.sleep(1)

        # 4. Start Background Worker
        print_header("Starting Broker Worker...")
        worker_proc = subprocess.Popen(
            [sys.executable, "-m", "broker.worker"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        PROCESSES.append(worker_proc)
        time.sleep(1)

        # 5. Start Broker API
        print_header("Starting Broker API Server...")
        api_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "broker.main:app", "--port", "8000"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        PROCESSES.append(api_proc)

        # Wait for API to be ready
        print("Waiting for API on http://localhost:8000...")
        for _ in range(30):
            try:
                urllib.request.urlopen("http://localhost:8000/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("Error: API server failed to start.")
            sys.exit(1)

        print_header("Broker local stack is fully operational! [SUCCESS]")
        print("You can open another terminal and run the 'osb' developer CLI tool.")
        print("Example commands:")
        print("  osb intent parse \"Configure circuit breaker for order-service with max connections 500\"")
        print("  osb intent apply [REQUEST_ID] --watch")
        print("  osb resources list")
        print("\nPress Ctrl+C to terminate the local stack.")

        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        cleanup()
    except Exception as e:
        print(f"Fatal error starting stack: {e}")
        cleanup()


if __name__ == "__main__":
    main()
