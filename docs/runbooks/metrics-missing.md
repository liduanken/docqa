# Runbook: DocQA metrics missing

**Alert:** `DocQAMetricsMissing` (page). Prometheus has no healthy DocQA scrape target, so every other DocQA alert is
blind.

## Triage

Start with the pods:

```bash
kubectl -n docqa get pods -l app.kubernetes.io/name=docqa
kubectl -n docqa describe pod <pod>
```

| Pod state | Likely cause |
|---|---|
| `CreateContainerConfigError` | The secret is missing. Check `kubectl -n docqa get externalsecret` (SecretSynced?) and the ClusterSecretStore. |
| `CrashLoopBackOff` | Bad configuration. Run `kubectl logs --previous`. Settings validation errors name the bad `DOCQA_*` variable. |
| `ImagePullBackOff` | Wrong image tag or missing pull secret. |
| Pending | Not enough capacity, or the topology constraints can't be met. |

If the pods are Running, check Prometheus itself:

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090
# http://localhost:9090/targets: is the docqa ServiceMonitor present? Is the target down?
kubectl -n docqa get servicemonitor docqa -o yaml   # needs the release=kube-prometheus-stack label
kubectl -n docqa get networkpolicy docqa -o yaml    # must admit the monitoring namespace on port http
```

## Mitigation

Fix the underlying cause from the table above. If a release caused it, run `helm -n docqa rollback docqa`.

## Escalation

If Prometheus itself is unhealthy, escalate to the observability on-call.
