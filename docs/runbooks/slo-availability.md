# Runbook: DocQA availability SLO

**Alerts:** `DocQAAvailabilityBudgetBurnFast` (page), `DocQAAvailabilityBudgetBurnMedium` (page), `DocQAAvailabilityBudgetBurnSlow` (ticket)

## SLO

99.5% of requests to real API routes return a non-5xx status, measured over 30 days.
SLI = `docqa:slo_availability_error_ratio:rate<window>` (5xx ÷ all, excluding unmatched routes).
The error budget is 0.5%. The fast burn (14.4×, 1h and 5m) spends about 2% of the monthly budget per hour.

## What it means

Users are getting server errors. In DocQA, almost all 5xx responses come from the LLM path, by design:

| Status | Error code | Usual cause | Go to |
|---|---|---|---|
| 503 | `llm_unavailable`, `llm_rate_limited`, `llm_circuit_open`, `llm_overloaded` | Provider outage, quota, or saturation | [llm-provider-degraded.md](llm-provider-degraded.md) |
| 504 | `llm_timeout` | Provider slow | [llm-provider-degraded.md](llm-provider-degraded.md) |
| 502 | `llm_error` | Provider rejected the request (bad key or model, 4xx) | Check the secret and model config |
| 500 | `internal_error` | Bug in DocQA | Find the `request_id` in the logs |
| 503 | `store_unavailable` (readyz) | Redis unreachable | Check Redis and the NetworkPolicy egress on 6379 |

## Triage

```promql
# Which status codes and routes?
sum by (code, route) (rate(docqa_http_requests_total{job="docqa"}[5m]))
# LLM outcome breakdown
sum by (outcome) (rate(docqa_llm_requests_total{job="docqa"}[5m]))
# Per-pod errors (one bad pod or all of them?)
sum by (pod) (rate(docqa_http_requests_total{job="docqa",code=~"5.."}[5m]))
```

```bash
kubectl -n docqa get pods -l app.kubernetes.io/name=docqa
kubectl -n docqa logs deploy/docqa --since=15m | grep -E '"(unhandled_error|llm_call_failed)"'
helm -n docqa history docqa          # did a release just go out?
```

For a 500, take the `x-request-id` from the client and search the logs for it. The log line also carries the
`trace_id` for the trace.

## Mitigation

- **Bad release:** `helm -n docqa rollback docqa <previous-revision>`.
- **Provider incident:** fail over to another provider or model (`config.DOCQA_LLM_BASE_URL` / `DOCQA_LLM_MODEL`), or wait.
  The circuit breaker is already protecting the provider.
- **Saturation (`llm_overloaded`):** scale out (`autoscaling.maxReplicas`) or raise `DOCQA_LLM_MAX_CONCURRENCY` within the provider quota.

## Escalation

If the issue is unresolved after 30 minutes of fast burn, escalate to the platform on-call lead. For provider incidents,
open a ticket with the provider and include request IDs and timestamps.
