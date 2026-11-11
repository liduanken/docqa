# Runbook: LLM provider degraded

**Alerts:** `DocQALLMCircuitOpen` (warning), `DocQALLMErrorRateHigh` (warning). This runbook usually accompanies an
availability burn page.

## How DocQA behaves when the provider struggles

| Provider condition | Service behaviour | Client status | Metric `outcome` |
|---|---|---|---|
| Slow (beyond `DOCQA_LLM_TIMEOUT_SECONDS`) | Retried with jittered backoff, then fails | 504 `llm_timeout` | `timeout` |
| Rate limited (429) | Retried, then fails with `Retry-After` | 503 `llm_rate_limited` | `rate_limited` |
| Down (5xx or connection error) | Retried, then fails with `Retry-After` | 503 `llm_unavailable` | `unavailable` |
| Rejects the request (other 4xx) | Not retried | 502 `llm_error` | `error` |
| Repeated retryable failures | Circuit opens and calls are rejected without reaching the provider | 503 `llm_circuit_open` | `circuit_open` |
| Too many concurrent calls | New calls are shed instead of queued | 503 `llm_overloaded` | `overloaded` |

Pods **stay Ready** during an LLM outage, because `/readyz` deliberately ignores LLM health. Uploads and health checks
keep working, and Kubernetes doesn't restart or drain healthy pods.

## Triage

```promql
sum by (outcome) (rate(docqa_llm_requests_total{job="docqa"}[5m]))
max by (pod) (docqa_llm_circuit_open{job="docqa"})
sum(rate(docqa_llm_tokens_total{job="docqa"}[1m])) * 60    # tokens/min vs your provider quota
```

```bash
kubectl -n docqa logs deploy/docqa --since=10m | grep llm_call_failed | tail
kubectl -n docqa get externalsecret docqa      # SecretSynced? A rotated or invalid key shows up as 502 llm_error
kubectl -n docqa get secret docqa-secrets -o jsonpath='{.data}' | grep -c DOCQA_LLM_API_KEY
```

Check the provider's status page.

## Mitigation

- **Expired or rotated key:** update it in Vault, then force a re-sync:
  `kubectl -n docqa annotate externalsecret docqa force-sync=$(date +%s) --overwrite`, then
  `kubectl -n docqa rollout restart deploy/docqa`. Pods read env vars only at startup.
- **Quota:** request a higher limit, or lower `DOCQA_LLM_MAX_CONCURRENCY` to stay under it.
- **Outage:** fail over by changing `config.DOCQA_LLM_BASE_URL` and `DOCQA_LLM_MODEL` in the environment values and running `helm upgrade`.
- **Injected fault:** if `DOCQA_FAKE_LLM_FAILURE_RATE` is set (demo or chaos testing), remove it.

## Escalation

Escalate to the provider with request IDs and timestamps. If the provider has no ETA, escalate to the platform lead
to decide on failover.
