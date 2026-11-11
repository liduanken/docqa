# DocQA guide: run it, demo it, troubleshoot it

## 1. What you get

- The API with a keyless fake LLM (`make run`), and tests and linters (`make test lint`).
- A kind cluster with kube-prometheus-stack, External Secrets, Vault (dev mode), DocQA, the operator and two
  example `DocQAService` resources.
- `scripts/demo.sh`, a guided demo that ends with a real burn-rate page firing.

## 2. Prerequisites

| Tool | Tested version |
|---|---|
| Python | 3.11, via [uv](https://docs.astral.sh/uv/) |
| Docker | Any recent version, with at least 4 CPUs and 8 GB of memory available |
| kind | Kubernetes 1.32 node image (the chart needs 1.30 or later) |
| Helm | 3.16 |
| kubectl | Matching the cluster |

## 3. Quick check without Kubernetes

```bash
make venv test lint
```

Expected: `34 passed` with about 90% coverage on `app`, `3 passed` for the operator, and ruff and mypy clean.

```bash
make run &
BASE_URL=http://localhost:8080 scripts/smoke.sh
```

Expected: an answer with one source, then a refusal (`"answerable": false`), then `413`.

## 4. Full cluster setup

| Step | Command | Time | Verify |
|---|---|---|---|
| Cluster | `make cluster` | ~1 min | `kubectl get nodes` |
| Platform add-ons | `make platform` | ~4 min | `kubectl get pods -n monitoring -n external-secrets -n vault` |
| Images | `make build` | ~2 min | `docker images \| grep docqa` |
| Service | `make deploy` | ~1 min | `kubectl -n docqa get externalsecret` shows `SecretSynced` |
| Smoke test | `make smoke` | seconds | answer, refusal, 413 |
| Operator | `make operator example` | ~1 min | `kubectl get docqa -A` shows both resources `Ready` |

To use a real LLM, export `LLM_API_KEY` before `make platform` / `make example` (it is written to Vault, never to
disk). Then set `config.DOCQA_LLM_PROVIDER=openai` in your values.

## 5. Running the demo

```bash
make demo                    # all sections, pausing between steps
AUTO=1 make demo             # unattended
make demo SECTIONS="1 4"     # selected sections
```

Section 0 (preflight) always runs. It checks the tools, re-seeds Vault if it has restarted, and starts the
port-forwards: the app on `:8080` and Prometheus on `:9090`.

| Section | Shows | Time |
|---|---|---|
| 1 Workload | smoke test, JSON logs without content | 2 min |
| 2 Packaging | Helm releases, dev vs prod values, ExternalSecret, securityContext | 3 min |
| 3 Custom resource | CRs, operator self-heal after deleting a Deployment, CEL rejection | 4 min |
| 4 Reliability | 80% injected LLM failures, then metrics, logs, Ready pods and `DocQAAvailabilityBudgetBurnFast` **firing** | 5–8 min |
| 5 Quality | tests, lint, CI walkthrough | 2 min |
| 6 Close | what I'd do next | 1 min |

Suggested screen layout: the terminal on the left, and the Prometheus alerts page (`http://localhost:9090/alerts`)
on the right. Rehearse once with `AUTO=1`.

## 6. Section walkthrough: what to point out

1. **Workload:** the refusal costs no LLM call. `x-request-id` appears in every log line. Show `app/main.py`
   (the `ask_question` handler) and `app/llm.py` (`SYSTEM_PROMPT`).
2. **Packaging:** the templates don't change per environment. Show the `secretRef` and `checksum/config` in
   `charts/docqa/templates/deployment.yaml`.
3. **Custom resource:** show the CEL rules in `docqa-operator/deploy/crd.yaml`, `spec_to_values()`, and the
   trade-off in the controller docstring.
4. **Reliability:** pods stay Ready during the outage. Show `app/resilience.py` (the retry loop and circuit breaker),
   `templates/prometheusrule.yaml`, and `docs/runbooks/slo-availability.md`.
5. **Quality:** `tests/test_resilience.py` and `.github/workflows/ci.yaml`.

## 7. Manual fallback commands

```bash
kubectl -n docqa port-forward svc/docqa 8080:80 &
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090 &
helm upgrade docqa charts/docqa -n docqa -f charts/docqa/values-dev.yaml \
  --set config.DOCQA_FAKE_LLM_FAILURE_RATE=0.8 --wait     # inject failures
DURATION=600 scripts/load.sh &                             # drive traffic
curl -s --noproxy localhost localhost:9090/api/v1/alerts | python3 -m json.tool | grep -E 'alertname|state'
helm upgrade docqa charts/docqa -n docqa -f charts/docqa/values-dev.yaml --wait   # restore
```

## 8. Behind a corporate proxy

- uv: `export UV_SYSTEM_CERTS=1 UV_DEFAULT_INDEX=https://<mirror>/simple`.
- Images: export the OS CA bundle and run `make build CA_BUNDLE=/path/ca.pem PIP_INDEX_URL=https://<mirror>/simple`.
  The CA is a BuildKit secret, and `HTTP(S)_PROXY` are passed as build args. Never disable TLS verification.
- kind can't pull through some proxies. In that case, `docker pull` the image on the host, then `kind load docker-image`.
- Use `curl --noproxy localhost` for port-forwards. The scripts already do this.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CreateContainerConfigError` | The Secret is missing | `kubectl -n docqa describe externalsecret docqa`. Vault may have restarted; re-run `make demo` (its preflight re-seeds Vault). |
| ExternalSecret "Secret does not exist" | Vault dev mode is in-memory, so a restart wiped it | Re-seed with `vault kv put`, then annotate `force-sync=$(date +%s)` |
| Seeding Vault fails with "container not found" | Vault isn't ready yet | `kubectl -n vault wait --for=condition=Ready pod/vault-0` |
| `curl` to localhost returns nothing | A proxy is intercepting it | `curl --noproxy localhost` |
| Port-forward dies after `helm upgrade` | The pod was replaced | Restart the port-forward |
| No DocQA targets in Prometheus | Selector or NetworkPolicy | See [metrics-missing.md](docs/runbooks/metrics-missing.md) |
| `DocQALLMCircuitOpen` rarely fires in the demo | The breaker cycles open → half-open every 30 s | Expected. The burn-rate page is the demo signal. |
| pip fails with `CERTIFICATE_VERIFY_FAILED` in `docker build` | A TLS-intercepting proxy | See section 8 |

## 10. Tear down

```bash
make clean
```
