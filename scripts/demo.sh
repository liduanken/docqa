#!/usr/bin/env bash
# Interactive interview demo. Usage: scripts/demo.sh [sections...]   (AUTO=1 skips pauses)
#   0 preflight (always runs)  1 workload  2 packaging  3 custom resource  4 reliability  5 quality  6 close
# Compatible with macOS bash 3.2: no bash-4 features, no "${arr[@]}" on possibly-empty arrays under set -u.
set -euo pipefail

cd "$(dirname "$0")/.."
NS=docqa
ENV=dev
AUTO="${AUTO:-0}"
INJECTED=0
APP_PF_PID=""
PROM_PF_PID=""
LOAD_PID=""

section() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
say() { printf '\033[33m» %s\033[0m\n' "$*"; }
ok() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✘ %s\033[0m\n' "$*"; }
pause() { if [ "$AUTO" != "1" ]; then read -r -p "  [enter] " _; fi; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; eval "$*"; }
http() { curl -s --noproxy localhost "$@"; }

port_forward() {  # name ns svc port -> sets <NAME>_PF_PID
  local name=$1 ns=$2 svc=$3 port=$4 target=80 pid
  if [ "$svc" != "svc/docqa" ]; then target=9090; fi
  pkill -f "port-forward.*$svc $port:" 2>/dev/null || true
  kubectl -n "$ns" port-forward "$svc" "$port:$target" >/dev/null 2>&1 &
  pid=$!
  eval "${name}_PF_PID=$pid"
  for _ in $(seq 1 30); do
    if http -o /dev/null "http://localhost:$port/" 2>/dev/null; then return 0; fi
    sleep 1
  done
  fail "port-forward to $svc did not come up"
}

cleanup() {
  if [ -n "$LOAD_PID" ]; then kill "$LOAD_PID" 2>/dev/null || true; fi
  if [ "$INJECTED" = "1" ]; then
    say "restoring the service (removing injected failures)"
    helm upgrade docqa charts/docqa -n "$NS" -f "charts/docqa/values-$ENV.yaml" --wait >/dev/null || true
  fi
  for pid in $APP_PF_PID $PROM_PF_PID; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

reseed_vault_if_needed() {
  # Vault dev mode is in-memory: a restarted vault-0 has lost every seeded key.
  local path
  for path in "docqa/$ENV" teams/claims/docqa; do
    if ! kubectl -n vault exec vault-0 -- vault kv get "secret/$path" >/dev/null 2>&1; then
      say "re-seeding secret/$path"
      kubectl -n vault exec vault-0 -- vault kv put "secret/$path" llm_api_key="${LLM_API_KEY:-}" >/dev/null
    fi
  done
  kubectl -n "$NS" annotate externalsecret docqa "force-sync=$(date +%s)" --overwrite >/dev/null 2>&1 || true
  kubectl -n team-claims annotate externalsecret claims-docqa "force-sync=$(date +%s)" --overwrite >/dev/null 2>&1 || true
}

s0() {
  section "0. Preflight"
  for tool in kubectl helm kind curl python3; do
    command -v "$tool" >/dev/null || { fail "missing $tool"; exit 1; }
  done
  kubectl cluster-info >/dev/null || { fail "no cluster: run make cluster platform build deploy"; exit 1; }
  reseed_vault_if_needed
  kubectl get pods -A --no-headers | grep -v -E 'Running|Completed' || true
  kubectl get docqaservices -A 2>/dev/null || true
  port_forward APP "$NS" svc/docqa 8080
  port_forward PROM monitoring svc/kube-prometheus-stack-prometheus 9090
  ok "app on :8080, Prometheus on :9090"
}

s1() {
  section "1. AI workload"
  say "Upload, answerable question, refusal, size limit. The fake LLM means no API key is needed."
  run "BASE_URL=http://localhost:8080 bash scripts/smoke.sh"
  pause
  say "Logs are JSON, correlated by request_id/trace_id, with no document content:"
  run "kubectl -n $NS logs deploy/docqa --tail=3"
  pause
}

s2() {
  section "2. Packaging and deployment"
  run "helm list -A"
  say "Same chart, different environments: only values differ."
  run "diff charts/docqa/values-dev.yaml charts/docqa/values-prod.yaml || true"
  pause
  say "The secret comes from Vault through External Secrets; the repo and chart never see it."
  run "kubectl -n $NS get externalsecret,secret"
  run "kubectl -n $NS get deploy docqa -o jsonpath='{.spec.template.spec.containers[0].securityContext}'; echo"
  pause
}

s3() {
  section "3. Custom resource"
  run "sed -n '1,25p' docqa-operator/examples/docqaservice.yaml"
  run "kubectl get docqaservices -A"
  run "helm list -n team-claims"
  pause
  say "Self-heal: delete the sandbox Deployment; the operator reinstalls it."
  run "kubectl -n team-claims delete deploy sandbox-docqa"
  local start i
  start=$(date +%s)
  for i in $(seq 1 20); do
    if kubectl -n team-claims get deploy sandbox-docqa >/dev/null 2>&1; then
      ok "back after $(( $(date +%s) - start ))s"
      break
    fi
    sleep 3
  done
  run "kubectl -n docqa-operator logs deploy/docqa-operator --tail=5 | grep -i -E 'sandbox|handler' || true"
  pause
  say "Validation: the fake LLM is rejected in prod by a CEL rule."
  run "cat <<'EOF' | kubectl apply -f - || true
apiVersion: platform.example.com/v1alpha1
kind: DocQAService
metadata: {name: invalid, namespace: team-claims}
spec: {team: claims, environment: prod, llm: {provider: fake}}
EOF"
  pause
}

s4() {
  section "4. Reliability and observability"
  say "Inject an 80% LLM failure rate."
  run "helm upgrade docqa charts/docqa -n $NS -f charts/docqa/values-$ENV.yaml --set config.DOCQA_FAKE_LLM_FAILURE_RATE=0.8 --wait"
  INJECTED=1
  say "The pod was replaced, so the port-forward died: re-establish it."
  port_forward APP "$NS" svc/docqa 8080
  DURATION=600 BASE_URL=http://localhost:8080 bash scripts/load.sh >/dev/null 2>&1 &
  LOAD_PID=$!
  if [ "$AUTO" != "1" ] && command -v open >/dev/null; then open http://localhost:9090/alerts; fi
  sleep 15
  run "http localhost:8080/metrics | grep -E '^docqa_llm_requests_total|^docqa_llm_circuit_open' || true"
  run "kubectl -n $NS logs deploy/docqa --tail=50 | grep llm_call_failed | tail -2 || true"
  run "kubectl -n $NS get pods"
  say "Pods stay Ready: readiness deliberately ignores LLM health."
  local i state=""
  for i in $(seq 1 40); do
    state=$(http "localhost:9090/api/v1/alerts" | python3 -c '
import json, sys
alerts = json.load(sys.stdin)["data"]["alerts"]
print(next((a["state"] for a in alerts if a["labels"]["alertname"] == "DocQAAvailabilityBudgetBurnFast"), "inactive"))')
    printf '  [%02d] DocQAAvailabilityBudgetBurnFast: %s\n' "$i" "$state"
    if [ "$state" = "firing" ]; then break; fi
    sleep 15
  done
  if [ "$state" = "firing" ]; then ok "page fired"; else fail "alert did not fire in time"; fi
  run "http localhost:9090/api/v1/alerts | python3 -c 'import json,sys; [print(a[\"state\"], a[\"labels\"][\"alertname\"], a[\"annotations\"].get(\"runbook_url\",\"\").rsplit(\"/\",1)[-1]) for a in json.load(sys.stdin)[\"data\"][\"alerts\"] if a[\"labels\"][\"alertname\"].startswith(\"DocQA\")]'"
  pause
  kill "$LOAD_PID" 2>/dev/null || true
  LOAD_PID=""
  say "Restore the service."
  run "helm upgrade docqa charts/docqa -n $NS -f charts/docqa/values-$ENV.yaml --wait >/dev/null"
  INJECTED=0
  port_forward APP "$NS" svc/docqa 8080
  ok "restored"
  pause
}

s5() {
  section "5. Quality and delivery"
  run "make test lint 2>&1 | grep -E 'passed|failed|TOTAL|Success|All checks' || true"
  say "CI: lint, tests, coverage gate, pip-audit; helm lint, kubeconform, promtool and server-side dry runs of the CRD;"
  say "gitleaks and Trivy; images built, scanned, SBOM'd and cosign-signed; chart pushed to OCI."
  pause
}

s6() {
  section "6. What I'd do next"
  say "Embeddings, a vector store and an eval set"
  say "Authentication, per-team rate limits and token budgets"
  say "Argo CD, and Argo Rollouts canaries gated on the SLO recording rules"
  say "Operator v2: server-side apply, owner references, narrower RBAC, admission webhook"
  say "Grafana/Sloth dashboards, image digest pinning, locked dependencies"
}

if [ $# -eq 0 ]; then set -- 1 2 3 4 5 6; fi
s0
for s in "$@"; do "s$s"; done
ok "demo complete"
