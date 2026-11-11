from app.rag import chunk_text, retrieve
from tests.conftest import DOC


def test_chunks_overlap_and_cover_all_words():
    text = " ".join(f"w{i}" for i in range(50))
    chunks = chunk_text(text, size=20, overlap=5)
    assert len(chunks) == 3
    assert chunks[0].split()[-5:] == chunks[1].split()[:5]
    assert chunks[-1].split()[-1] == "w49"


def test_short_text_is_one_chunk():
    assert chunk_text("just a few words", size=20, overlap=5) == ["just a few words"]


def test_retrieval_ranks_relevant_chunk_first():
    chunks = chunk_text(DOC, size=20, overlap=5)
    hits = retrieve(chunks, "How often are Kubernetes clusters upgraded?", k=3)
    assert "Kubernetes" in hits[0].text
    assert hits[0].score > hits[-1].score


def test_stopword_only_question_returns_nothing():
    assert retrieve(["some text"], "what is the", k=3) == []
