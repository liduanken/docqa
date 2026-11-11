CLUSTER ?= docqa
ENV ?= dev
NAMESPACE ?= docqa
VERSION ?= 0.1.0
IMAGE ?= docqa:$(VERSION)
OPERATOR_IMAGE ?= docqa-operator:$(VERSION)
PY ?= .venv/bin/python
# Behind a TLS-intercepting proxy: make build CA_BUNDLE=/path/to/ca.pem PIP_INDEX_URL=https://mirror/simple
CA_BUNDLE ?=
PIP_INDEX_URL ?=
SECTIONS ?=

comma := ,
BUILD_OPTS = $(if $(CA_BUNDLE),--secret id=ca_bundle$(comma)src=$(CA_BUNDLE),) \
  $(if $(PIP_INDEX_URL),--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL),) \
  $(foreach v,HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy,$(if $($(v)),--build-arg $(v)=$($(v)),))

.PHONY: help venv test lint run cluster platform build deploy smoke demo operator example template clean

help: ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

venv: ## Create .venv with app, operator and dev tools
	uv venv --python 3.11 --clear .venv && uv pip install -e . --group dev -e "./docqa-operator[dev]"

test: ## Unit tests (service with coverage, then operator)
	$(PY) -m pytest tests --cov=app --cov-report=term-missing
	cd docqa-operator && ../$(PY) -m pytest

lint: ## ruff + mypy
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy app

run: ## Run the API locally on :8080 with the fake LLM
	$(PY) -m uvicorn app.main:create_app --factory --port 8080

cluster: ## Create the kind cluster
	kind create cluster --name $(CLUSTER) --wait 60s

platform: ## Install kube-prometheus-stack, external-secrets and Vault (dev mode)
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo add external-secrets https://charts.external-secrets.io
	helm repo add hashicorp https://helm.releases.hashicorp.com
	helm repo update
	helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
	  -n monitoring --create-namespace --wait --set grafana.enabled=false \
	  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
	  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false
	helm upgrade --install external-secrets external-secrets/external-secrets \
	  -n external-secrets --create-namespace --wait
	helm upgrade --install vault hashicorp/vault -n vault --create-namespace \
	  --set server.dev.enabled=true --set injector.enabled=false
	kubectl -n external-secrets create secret generic vault-token --from-literal=token=root \
	  --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f deploy/local/cluster-secret-store.yaml
	kubectl -n vault wait --for=condition=Ready pod/vault-0 --timeout=10m
	kubectl -n vault exec vault-0 -- vault kv put secret/docqa/$(ENV) llm_api_key="$${LLM_API_KEY:-}"

build: ## Build both images and load them into kind
	docker build $(BUILD_OPTS) -t $(IMAGE) .
	docker build $(BUILD_OPTS) -f docqa-operator/Dockerfile -t $(OPERATOR_IMAGE) .
	kind load docker-image $(IMAGE) $(OPERATOR_IMAGE) --name $(CLUSTER)

deploy: ## helm upgrade --install for ENV (dev|prod)
	helm upgrade --install docqa charts/docqa -n $(NAMESPACE) --create-namespace \
	  -f charts/docqa/values-$(ENV).yaml --set image.tag=$(VERSION) --wait --timeout 3m
	kubectl -n $(NAMESPACE) get pods,externalsecret

smoke: ## Port-forward and run scripts/smoke.sh
	kubectl -n $(NAMESPACE) port-forward svc/docqa 8080:80 >/dev/null 2>&1 & PF=$$!; \
	  sleep 3; BASE_URL=http://localhost:8080 bash scripts/smoke.sh; RC=$$?; kill $$PF; exit $$RC

demo: ## Interactive interview demo (AUTO=1 skips pauses; SECTIONS="1 4")
	bash scripts/demo.sh $(SECTIONS)

operator: ## Install the CRD and the operator
	kubectl apply -f docqa-operator/deploy/crd.yaml
	kubectl wait --for=condition=Established crd/docqaservices.platform.example.com --timeout=60s
	kubectl apply -f docqa-operator/deploy/operator.yaml
	kubectl -n docqa-operator rollout status deploy/docqa-operator --timeout=120s

example: ## Apply the example DocQAService resources
	kubectl create namespace team-claims --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n vault exec vault-0 -- vault kv put secret/teams/claims/docqa llm_api_key="$${LLM_API_KEY:-}"
	kubectl apply -f docqa-operator/examples/docqaservice.yaml
	kubectl -n team-claims get docqaservices

template: ## Render the chart for dev and prod
	helm template docqa charts/docqa -f charts/docqa/values-dev.yaml
	helm template docqa charts/docqa -f charts/docqa/values-prod.yaml

clean: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)
