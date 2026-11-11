# Runbook: LLM cost high

**Alert:** `DocQALLMCostHigh` (warning). Estimated spend over the last hour exceeds `slo.llm.costPerHourWarningUsd`.

Cost is estimated from token counts × `DOCQA_LLM_PRICE_*_PER_1K`. It is an operator signal, not a billing source of truth.

## Triage

```promql
sum(rate(docqa_http_requests_total{job="docqa",route="/v1/documents/{document_id}/questions"}[1h]))  # traffic
sum by (type) (rate(docqa_llm_tokens_total{job="docqa"}[1h]))                                      # token mix
sum(rate(docqa_llm_tokens_total{job="docqa",type="prompt"}[1h]))
  / sum(rate(docqa_llm_requests_total{job="docqa",outcome="success"}[1h]))                          # tokens per call
sum by (model) (rate(docqa_llm_cost_usd_total{job="docqa"}[1h])) * 3600                            # USD/h by model
```

Ask three questions:

- **Is traffic up?** If so, is it organic, or is a single client spiking? Check ingress logs by client.
- **Are tokens per request up?** Look for larger `DOCQA_TOP_K` or chunk sizes, or longer questions.
- **Did the model change?** A more expensive model may have been rolled out (`helm -n docqa history docqa`).

## Mitigation

- Roll back an unintended model or configuration change.
- Rate-limit the offending client at the ingress.
- Reduce context: lower `DOCQA_TOP_K` or `DOCQA_CHUNK_SIZE_WORDS`.
- If the spend is expected, raise the threshold in the environment values.

## Escalation

If spend is unexplained and continuing, notify the owning team and FinOps.
