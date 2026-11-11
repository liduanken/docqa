from app.llm import NOT_FOUND
from app.main import CANNOT_ANSWER
from tests.conftest import MockLLM, upload


def _ask(client, doc_id, question):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/documents/{doc_id}/questions", json={"question": question})


def test_mocked_llm_happy_path(make_client):
    llm = MockLLM("It is in Canary Wharf.")
    client = make_client(llm)
    resp = _ask(client, upload(client), "Where is the company headquarters?")
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "It is in Canary Wharf."
    assert body["answerable"] is True
    assert body["model"] == "mock-1"
    assert any("Canary Wharf" in s["text"] for s in body["sources"])
    _, user_prompt = llm.calls[0]
    assert "Canary Wharf" in user_prompt
    assert "Where is the company headquarters?" in user_prompt


def test_irrelevant_question_refused_without_llm_call(make_client):
    llm = MockLLM()
    client = make_client(llm)
    resp = _ask(client, upload(client), "Favourite pizza topping?")
    assert resp.json() == {"answer": CANNOT_ANSWER, "answerable": False, "sources": [], "model": "mock-1"}
    assert llm.calls == []


def test_llm_not_found_becomes_refusal(make_client):
    client = make_client(MockLLM(NOT_FOUND))
    body = _ask(client, upload(client), "When was the headquarters renovated?").json()
    assert body["answerable"] is False
    assert body["answer"] == CANNOT_ANSWER


def test_fake_llm_end_to_end_without_api_key(make_client):
    client = make_client()  # default provider is the keyless fake
    body = _ask(client, upload(client), "When was Barclays founded?").json()
    assert body["answerable"] is True
    assert "1690" in body["answer"]


def test_metrics_exposed(make_client):
    client = make_client(MockLLM("Canary Wharf."), llm_price_prompt_per_1k=0.001)
    _ask(client, upload(client), "Where is the headquarters?")
    metrics = client.get("/metrics").text
    assert 'docqa_llm_requests_total{model="mock-1",outcome="success",provider="mock"}' in metrics
    assert 'docqa_llm_tokens_total{model="mock-1",provider="mock",type="prompt"}' in metrics
    assert 'docqa_llm_cost_usd_total{model="mock-1",provider="mock"}' in metrics
    assert 'route="/v1/documents/{document_id}/questions"' in metrics


def test_logs_are_structured_and_free_of_content(make_client, capsys):
    client = make_client(MockLLM("It is in Canary Wharf."))
    _ask(client, upload(client), "Where is the headquarters?")
    out = capsys.readouterr().out
    assert '"event": "question_answered"' in out
    assert "Canary Wharf" not in out
    assert "headquarters" not in out
