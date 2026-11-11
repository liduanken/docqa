"""Pure mapping from a DocQAService spec to Helm values. No Kubernetes calls, so it is unit-tested."""

from __future__ import annotations

import math
from typing import Any

SIZES: dict[str, dict[str, Any]] = {
    "small": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "256Mi"}},
    "medium": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "512Mi"}},
    "large": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
}
DEFAULT_MODELS = {"fake": "fake-extractive-1", "openai": "gpt-4o-mini"}


def release_name(name: str) -> str:
    return name if "docqa" in name else f"{name}-docqa"


def spec_to_values(
    spec: dict[str, Any], *, image: str, redis_remote_key: str = "platform/docqa/redis"
) -> dict[str, Any]:
    repository, _, tag = image.rpartition(":")
    llm = spec["llm"]
    replicas = spec.get("replicas", {})
    rmin, rmax = replicas.get("min", 1), replicas.get("max", 1)
    docs = spec.get("documents", {})
    max_bytes = docs.get("maxBytes", 100_000)
    # More than one replica needs a shared store, or questions miss documents uploaded to other pods.
    shared_store = rmax > 1

    config: dict[str, str] = {
        "DOCQA_LLM_PROVIDER": llm["provider"],
        "DOCQA_LLM_MODEL": llm.get("model") or DEFAULT_MODELS[llm["provider"]],
        "DOCQA_LLM_TIMEOUT_SECONDS": str(llm.get("timeoutSeconds", 10)),
        "DOCQA_LLM_MAX_CONCURRENCY": str(llm.get("maxConcurrency", 16)),
        "DOCQA_MAX_DOCUMENT_BYTES": str(max_bytes),
        "DOCQA_DOCUMENT_TTL_SECONDS": str(docs.get("ttlHours", 24) * 3600),
        "DOCQA_STORE_BACKEND": "redis" if shared_store else "memory",
    }
    if llm.get("baseUrl"):
        config["DOCQA_LLM_BASE_URL"] = llm["baseUrl"]

    secret_data: list[dict[str, Any]] = []
    if creds := llm.get("credentials"):
        secret_data.append(
            {
                "secretKey": "DOCQA_LLM_API_KEY",
                "remoteRef": {"key": creds["remoteKey"], "property": creds.get("property", "llm_api_key")},
            }
        )
    if shared_store:
        secret_data.append(
            {"secretKey": "DOCQA_REDIS_URL", "remoteRef": {"key": redis_remote_key, "property": "redis_url"}}
        )

    slo = spec.get("slo", {})
    host = spec.get("ingress", {}).get("host")
    ingress: dict[str, Any] = {"enabled": False}
    if host:
        # Worst-case JSON escaping (6x) plus slack, matching the app's early 413 guard.
        body_kb = math.ceil((6 * max_bytes + 4096) / 1024)
        ingress = {
            "enabled": True,
            "host": host,
            "annotations": {"nginx.ingress.kubernetes.io/proxy-body-size": f"{body_kb}k"},
        }

    return {
        "team": spec["team"],
        "image": {"repository": repository, "tag": tag},
        "resources": SIZES[spec.get("size", "small")],
        "replicaCount": rmin,
        "autoscaling": {"enabled": rmax > rmin, "minReplicas": rmin, "maxReplicas": rmax},
        "podDisruptionBudget": {"enabled": rmin > 1},
        "config": config,
        "secrets": {
            "optional": not secret_data,
            "externalSecret": {"enabled": bool(secret_data), "data": secret_data},
        },
        "slo": {
            "enabled": True,
            "availability": {"target": slo.get("availabilityTarget", 0.995)},
            "latency": {
                "target": slo.get("latencyTarget", 0.95),
                "thresholdSeconds": slo.get("latencyThresholdSeconds", "2.5"),
            },
            "llm": {"costPerHourWarningUsd": slo.get("llmCostPerHourWarningUsd", 5)},
        },
        "ingress": ingress,
    }
