# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.11-slim

FROM ${PYTHON_IMAGE} AS build
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_INDEX_URL=${PIP_INDEX_URL}
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml ./
COPY app ./app
# Optional corporate CA (TLS-intercepting proxies); a secret mount keeps it out of the image layers.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi && pip install . \
    # Build tooling has no place at runtime (and setuptools vendors packages with known CVEs).
    && pip uninstall -y setuptools pip

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Pick up Debian security fixes released after the base image was built, and drop the base
# image's own build tooling.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y setuptools wheel pip \
    && groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
EXPOSE 8080
# One worker per pod: scale with replicas, not processes.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-graceful-shutdown", "20", "--no-server-header", "--no-access-log", "--log-level", "warning"]
