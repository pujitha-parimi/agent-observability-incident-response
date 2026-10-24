# Agent incident response runbook

## Symptoms and detections

Typical signals for an agent regression or false-positive incident include:

- elevated error rate in Prometheus
- increased p95 latency
- unexpected guardrail blocks
- tool failures or retries
- trace spans marked with status=ERROR

## Severity classification

- SEV-1: repeated high error rate or service outage; user-visible failures
- SEV-2: elevated latency or partial agent failure; degraded but not fully blocked
- SEV-3: noisy but non-critical false positives or minor regression

## Initial triage

1. Check the app health endpoint at `http://localhost:8000/health` and metrics at `http://localhost:8000/metrics`.
2. Inspect Prometheus targets at http://localhost:9090/targets.
3. Review recent traces in the local console exporter or OTLP endpoint.
4. Validate whether the issue is caused by invalid input, a tool failure, model failure, or guardrail policy.

## Inspect metrics and traces

Prometheus queries:

```promql
sum(rate(agent_requests_total{status="error"}[5m]))
/
clamp_min(sum(rate(agent_requests_total[5m])), 0.001)
```

```promql
histogram_quantile(0.95, sum(rate(agent_request_latency_seconds_bucket[5m])) by (le))
```

```promql
sum(rate(agent_tool_failures_total[5m])) by (tool_name, status)
```

Trace review:

- look for `agent.request` spans with `agent.status=error`
- inspect child spans such as `agent.execute_tool`, `agent.model_call`, and `agent.validate_input`
- confirm exceptions were recorded with sanitized `error.type` and `error.message` values

## Identify failure types

- invalid input: `ValueError` from empty or whitespace questions
- tool failure: `RuntimeError` during local retrieval or policy checks
- guardrail rejection: `PermissionError` for blocked unsafe content
- unexpected exception: any unhandled error recorded by `record_exception`

## Reproduce the incident

Use the local deterministic agent directly:

```python
from agent.agent import run_agent

print(run_agent(""))
print(run_agent("fail this request"))
print(run_agent("drop table users"))
```

## Immediate mitigation

- stop the automations causing repeated invalid requests
- temporarily disable or narrow the failing tool path
- reduce request volume or protect the service by rejecting unsafe prompts early
- revert to the last known-good configuration or commit

## Rollback procedure

1. Stop the running local app process.
2. Check out the prior working version of the code in the project.
3. Restart the service and validate the metrics endpoint.
4. Re-run the deterministic regression suite.

## Verification after recovery

```bash
pytest -q
```

Then confirm:

- `agent_requests_total` increments for both success and error cases
- `agent_request_latency_seconds` is recorded
- no raw secrets or prompts appear in logs or traces
- Prometheus targets remain healthy

## Post-incident follow-up

- add or improve the regression test that triggered the incident
- record the root cause in the incident document
- review alert thresholds and tune for noise if needed
- keep the runbook and alert conditions aligned with the emitted metric names and labels
