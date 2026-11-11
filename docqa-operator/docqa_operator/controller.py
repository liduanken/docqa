"""Kopf controller: reconciles DocQAService custom resources into Helm releases of charts/docqa.

Helm-operator pattern: the chart stays the single source of truth for manifests, and the operator
only translates a team's intent (the CR spec) into values. Trade-off: the operator needs broad RBAC
(Helm stores releases in Secrets) and the objects get no ownerReferences to the CR. A v2 would use
native server-side apply with owner references.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import kopf
import kubernetes
from kubernetes.client.rest import ApiException

from docqa_operator.values import release_name, spec_to_values

GROUP, VERSION, PLURAL = "platform.example.com", "v1alpha1", "docqaservices"
CHART_PATH = Path(os.environ.get("DOCQA_CHART_PATH", "/chart"))
IMAGE = os.environ.get("DOCQA_IMAGE", "docqa:0.1.0")
REDIS_REMOTE_KEY = os.environ.get("DOCQA_REDIS_REMOTE_KEY", "platform/docqa/redis")
HELM_BIN = os.environ.get("HELM_BIN", "/usr/local/bin/helm")  # absolute path: no PATH lookup (ruff S607)
HELM_TIMEOUT_SECONDS = 120


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.persistence.finalizer = "platform.example.com/docqa-finalizer"
    settings.posting.level = 20  # INFO: post progress as Events on the CR
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


def _helm(*args: str) -> str:
    proc = subprocess.run(  # noqa: S603 (fixed binary, argument list, no shell)
        [HELM_BIN, *args], capture_output=True, text=True, timeout=HELM_TIMEOUT_SECONDS, check=False
    )
    if proc.returncode != 0:
        # Temporary: Kopf retries later and posts the error as an Event on the CR.
        raise kopf.TemporaryError(proc.stderr.strip()[-500:], delay=30)
    return proc.stdout


def _install(name: str, namespace: str, spec: dict[str, Any]) -> int:
    env = spec.get("environment", "dev")
    baseline = CHART_PATH / f"values-{env}.yaml"
    if not baseline.exists():
        raise kopf.PermanentError(f"no baseline values for environment {env!r}")
    values = spec_to_values(spec, image=IMAGE, redis_remote_key=REDIS_REMOTE_KEY)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(values, fh)
        values_file = fh.name
    try:
        out = _helm(
            "upgrade", "--install", release_name(name), str(CHART_PATH),
            "-n", namespace, "-f", str(baseline), "-f", values_file,
            "--history-max", "5", "--output", "json",
        )  # fmt: skip
    finally:
        os.unlink(values_file)
    return int(json.loads(out)["version"])


def _require_namespace(namespace: str | None) -> str:
    if not namespace:  # the CRD is namespaced, so this only guards against misuse
        raise kopf.PermanentError("DocQAService must be namespaced")
    return namespace


def _condition(type_: str, status: bool, reason: str, message: str = "") -> dict[str, str]:
    return {
        "type": type_,
        "status": "True" if status else "False",
        "reason": reason,
        "message": message,
        "lastTransitionTime": datetime.now(UTC).isoformat(timespec="seconds"),
    }


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(
    name: str, namespace: str | None, spec: kopf.Spec, meta: kopf.Meta, patch: kopf.Patch, **_: Any
) -> None:
    namespace = _require_namespace(namespace)
    revision = _install(name, namespace, dict(spec))
    release = release_name(name)
    patch.status.update(
        {
            "phase": "Progressing",
            "observedGeneration": meta.get("generation"),
            "helmRevision": revision,
            "endpoint": f"http://{release}.{namespace}.svc.cluster.local",
            "conditions": [_condition("Reconciled", True, "HelmReleaseApplied", f"revision {revision}")],
        }
    )


@kopf.timer(GROUP, VERSION, PLURAL, interval=30, initial_delay=10, idle=10)
def report_status(name: str, namespace: str | None, spec: kopf.Spec, patch: kopf.Patch, **_: Any) -> None:
    namespace = _require_namespace(namespace)
    release = release_name(name)
    apps = kubernetes.client.AppsV1Api()
    try:
        deployment = apps.read_namespaced_deployment_status(release, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        # Self-heal: someone deleted the Deployment, so reinstall the release.
        _install(name, namespace, dict(spec))
        patch.status["phase"] = "Progressing"
        return
    status = deployment.status
    if status is None:
        patch.status["phase"] = "Progressing"
        return
    ready = status.ready_replicas or 0
    desired = status.replicas or 0
    if ready and ready >= desired:
        phase = "Ready"
    elif ready == 0 and desired > 0 and (status.unavailable_replicas or 0) >= desired:
        phase = "Degraded"
    else:
        phase = "Progressing"
    patch.status.update(
        {
            "readyReplicas": ready,
            "phase": phase,
            "conditions": [
                _condition("Reconciled", True, "HelmReleaseApplied"),
                _condition("Ready", phase == "Ready", phase, f"{ready}/{desired} replicas ready"),
            ],
        }
    )


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete(name: str, namespace: str | None, **_: Any) -> None:
    namespace = _require_namespace(namespace)
    _helm("uninstall", release_name(name), "-n", namespace, "--ignore-not-found", "--wait")
