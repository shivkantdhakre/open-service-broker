# Production Runbook: SQS Dead Letter Queue (DLQ) Triage & Recovery

**Doc ID**: RB-OSB-001  
**Severity**: HIGH (P2)  
**Target Audience**: DevOps Engineers, Site Reliability Engineers (SREs), Platform Engineers  
**Applies To**: Advanced AI Service Broker Asynchronous Pipeline

---

## 1. Overview & Alert Description

This runbook describes the operational procedures for triaging and resolving alerts related to messages accumulating in the Dead Letter Queue (DLQ).

### Alert Trigger
- **Alarm Name**: `SQS-DLQ-Backlog-Alert`
- **Condition**: `ApproximateNumberOfMessagesVisible >= 1` for 5 consecutive minutes on `broker-tasks-dlq`.
- **Event Bus Notification**: A high-priority `"anomaly"` event is emitted to the platform EventBus containing:
  - `task_id`: SQS task identifier
  - `correlation_id`: Request correlation trace ID
  - `resource_id`: Failed DynamoDB resource ID (ULID)
  - `error`: Error details logged by the worker

### Production Impact
Tasks in the DLQ represent failed provisioning requests. The corresponding cloud infrastructure is in a `FAILED` state, and the developer's request has stalled. No auto-retries remain for these messages on the queue.

---

## 2. Manual Triage Steps

When the DLQ backlog alarm fires, follow these steps to isolate the root cause.

### Step 1: Extract Correlation Details
1. Connect to the event dashboard or query the alert notification payload.
2. Retrieve the **Correlation ID** (e.g. `corr-xxx`), **Resource ID** (ULID), and **Task ID**.

### Step 2: Query Centralized Trace Logs
Search CloudWatch Logs (or Datadog/ELK) using the correlation ID to reconstruct the execution trace:
```sql
fields @timestamp, @message, level, correlation_id
| filter correlation_id = "YOUR_CORRELATION_ID"
| sort @timestamp desc
```
Look for:
- Sovereign API return codes (e.g., HTTP 400 Bad Request, HTTP 503 Control Plane Downtime).
- Network timeouts between the worker and `http://sovereign:8080`.

### Step 3: Inspect DynamoDB State
Query the DynamoDB table to retrieve the exact configuration and error log recorded by the worker:
```bash
aws dynamodb get-item \
    --table-name broker-resources \
    --key '{"resource_id": {"S": "RESOURCE_ID"}, "resource_type": {"S": "ACTION_TYPE"}}'
```
Examine the `error_message` and the `configuration` parameters.

---

## 3. Recovery Procedures

Once the root cause is diagnosed, select one of the following recovery strategies.

### Option A: Trigger LLM-Driven Auto-Healing (Recommended)
If the failure was due to an invalid configuration parameter that passed initial parsing but was rejected by Sovereign (e.g. invalid target cluster weight split or naming clash):
1. Invoke the API's auto-retry endpoint to trigger the Auto-Retry Agent:
   ```bash
   curl -X POST https://api.broker.platform.internal/api/v1/resources/RESOURCE_ID/auto-retry \
     -H "Content-Type: application/json" \
     -H "X-API-Key: YOUR_API_KEY"
   ```
2. The Auto-Retry Agent will:
   - Request the LLM to diagnose the error and rewrite the configuration.
   - Scan the fix against OPA compliance rules.
   - Update the configuration in DynamoDB and reset the state to `PENDING`.
   - Re-enqueue a new task to SQS.
3. Monitor the EventBus or logs using the new request ID to confirm success.

### Option B: SQS Redrive (Transient / Network Errors)
If the logs indicate the failure was due to transient infrastructure issues (e.g., Sovereign API downtime, temporary DB throttling) that have since resolved:
1. In the AWS Console, navigate to **Simple Queue Service** -> **broker-tasks-dlq**.
2. Click **Start DLQ Redrive**.
3. Select **Redrive to source queue** and click **Redrive**.
4. Alternatively, use the AWS CLI to move messages back:
   - *Note: Ensure your worker is running and healthy before initiating the redrive.*

### Option C: Manual Database Correction
If Option A fails OPA checks and Option B is not applicable (requires a custom parameter fix):
1. Update the configuration parameter values directly in DynamoDB.
2. Atomically reset the state to `PENDING` and increment the record version:
   ```bash
   aws dynamodb update-item \
       --table-name broker-resources \
       --key '{"resource_id": {"S": "RESOURCE_ID"}, "resource_type": {"S": "ACTION_TYPE"}}' \
       --update-expression "SET #state = :pending, version = version + :one REMOVE error_message" \
       --expression-attribute-names '{"#state": "state"}' \
       --expression-attribute-values '{":pending": {"S": "PENDING"}, ":one": {"N": "1"}}'
   ```
3. Enqueue a manual SQS task representing the provisioning update:
   ```bash
   aws sqs send-message \
       --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/broker-tasks \
       --message-body '{"task_id": "RESOURCE_ID", "task_type": "provision", "resource_id": "RESOURCE_ID", "configuration": {...}}'
   ```

---

## 4. Escalation Paths

If the queue remains backlogged after trying the above procedures:
1. **Infrastructure Issue**: Contact the Platform/Infra team on Slack channel `#help-platform-infra`.
2. **AI Gateway/LLM Hallucinations**: Contact the AI Platform team on Slack channel `#help-ai-brain`.
