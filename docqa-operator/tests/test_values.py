from docqa_operator.values import release_name, spec_to_values

IMAGE = "registry.example.com/docqa:1.2.3"


def test_minimal_fake_spec():
    values = spec_to_values({"team": "claims", "llm": {"provider": "fake"}}, image=IMAGE)
    assert values["image"] == {"repository": "registry.example.com/docqa", "tag": "1.2.3"}
    assert values["config"]["DOCQA_LLM_MODEL"] == "fake-extractive-1"
    assert values["config"]["DOCQA_STORE_BACKEND"] == "memory"
    assert values["secrets"] == {"optional": True, "externalSecret": {"enabled": False, "data": []}}
    assert values["autoscaling"]["enabled"] is False
    assert values["podDisruptionBudget"]["enabled"] is False
    assert values["ingress"] == {"enabled": False}


def test_full_spec():
    spec = {
        "team": "claims",
        "environment": "prod",
        "size": "medium",
        "replicas": {"min": 2, "max": 5},
        "llm": {
            "provider": "openai",
            "model": "gpt-4o",
            "timeoutSeconds": 8,
            "credentials": {"remoteKey": "teams/claims/docqa"},
        },
        "documents": {"maxBytes": 200000, "ttlHours": 2},
        "slo": {"availabilityTarget": 0.999, "latencyThresholdSeconds": "5.0"},
        "ingress": {"host": "claims-docqa.example.com"},
    }
    values = spec_to_values(spec, image=IMAGE, redis_remote_key="platform/redis")
    assert values["autoscaling"] == {"enabled": True, "minReplicas": 2, "maxReplicas": 5}
    assert values["podDisruptionBudget"]["enabled"] is True
    assert values["resources"]["requests"]["cpu"] == "250m"
    config = values["config"]
    assert config["DOCQA_LLM_MODEL"] == "gpt-4o"
    assert config["DOCQA_MAX_DOCUMENT_BYTES"] == "200000"
    assert config["DOCQA_DOCUMENT_TTL_SECONDS"] == "7200"
    assert config["DOCQA_STORE_BACKEND"] == "redis"
    data = values["secrets"]["externalSecret"]["data"]
    assert [d["secretKey"] for d in data] == ["DOCQA_LLM_API_KEY", "DOCQA_REDIS_URL"]
    assert data[0]["remoteRef"] == {"key": "teams/claims/docqa", "property": "llm_api_key"}
    assert values["secrets"]["optional"] is False
    assert values["slo"]["availability"]["target"] == 0.999
    assert values["slo"]["latency"]["thresholdSeconds"] == "5.0"
    assert values["ingress"]["annotations"]["nginx.ingress.kubernetes.io/proxy-body-size"] == "1176k"


def test_release_name():
    assert release_name("claims-docqa") == "claims-docqa"
    assert release_name("sandbox") == "sandbox-docqa"
