# syntax=docker/dockerfile:1.7
# Build context is the repo root (the chart is baked into the image).
ARG PYTHON_IMAGE=python:3.11-slim

FROM ${PYTHON_IMAGE} AS helm
ARG HELM_VERSION=v3.16.2
# Set by BuildKit to the target architecture (amd64, arm64), so Apple Silicon kind clusters work too.
ARG TARGETARCH
ADD https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz /tmp/helm.tgz
ADD https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz.sha256sum /tmp/helm.tgz.sha256sum
RUN cd /tmp && echo "$(cut -d' ' -f1 helm.tgz.sha256sum)  helm.tgz" | sha256sum -c - \
    && tar xzf helm.tgz "linux-${TARGETARCH}/helm" && mv "linux-${TARGETARCH}/helm" /usr/local/bin/helm

FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HELM_CACHE_HOME=/tmp/helm/cache HELM_CONFIG_HOME=/tmp/helm/config HELM_DATA_HOME=/tmp/helm/data \
    HELM_BIN=/usr/local/bin/helm DOCQA_CHART_PATH=/chart
COPY --from=helm /usr/local/bin/helm /usr/local/bin/helm
WORKDIR /src
COPY docqa-operator/pyproject.toml ./
COPY docqa-operator/docqa_operator ./docqa_operator
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi && pip install .
COPY charts/docqa /chart
# Not "operator": that user already exists in Debian (useradd exit code 9).
RUN useradd --system --uid 10001 docqa-operator
USER 10001
CMD ["kopf", "run", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "-m", "docqa_operator.controller"]
