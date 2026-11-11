# Runbook: DocQA latency SLO

**Alerts:** `DocQALatencyBudgetBurnFast` (page), `DocQALatencyBudgetBurnMedium` (page), `DocQALatencyBudgetBurnSlow` (ticket)

## SLO

95% of question requests (`POST /v1/documents/{document_id}/questions`) complete within 2.5 s, measured over 30 days.
SLI = `docqa:slo_latency_error_ratio:rate<window>` (requests slower than the threshold ÷ all question requests).
The threshold must equal a histogram bucket (`slo.latency.thresholdSeconds`).

## What it means

Answers are slow. Question latency is dominated by the LLM call, including retries.

## Triage

```promql
# p95 of HTTP vs LLM latency: if they track each other, the provider is the bottleneck
histogram_quantile(0.95, sum by (le) (rate(docqa_http_request_duration_seconds_bucket{job="docqa",route="/v1/documents/{document_id}/questions"}[5m])))
histogram_quantile(0.95, sum by (le) (rate(docqa_llm_request_duration_seconds_bucket{job="docqa"}[5m])))
# Retries inflate latency: are timeouts or rate limits being retried?
sum by (outcome) (rate(docqa_llm_requests_total{job="docqa"}[5m]))
# CPU throttling
sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace="docqa",container="docqa"}[5m]))
```

```bash
kubectl -n docqa top pods
```

In the tracing backend, open slow `POST /v1/documents/{document_id}/questions` spans and compare `llm.generate`
duration and `retry` events with `rag.retrieve`.

## Mitigation

- **Slow provider:** lower `DOCQA_LLM_TIMEOUT_SECONDS` and `DOCQA_LLM_MAX_RETRIES`. This is a deliberate trade:
  requests fail faster, spending availability budget to protect latency.
- Switch to a faster model, or send less context (lower `DOCQA_TOP_K` or `DOCQA_CHUNK_SIZE_WORDS`).
- **CPU throttling:** raise the CPU request or scale out.

## Escalation

As for availability. If the provider is the cause, follow [llm-provider-degraded.md](llm-provider-degraded.md).
