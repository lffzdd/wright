from types import SimpleNamespace

from wright.application.memory.turn_snapshot import real_user_texts, turn_evidence
from wright.domain.model.memory.episode import EvidenceRef
from wright.domain.model.session import USER_INTERJECTION_SOURCE, Session
from wright.infrastructure.persistence.memory.evidence import SessionEvidenceSource


def _session(tmp_path):
    session = Session.create("goal", tmp_path)
    session.begin_user_turn("migrate")
    session.append_user_message("migrate the config")
    session.append_user_message("we always keep YAML", source=USER_INTERJECTION_SOURCE)
    return session


def test_interjections_count_as_user_statements_of_the_turn(tmp_path):
    session = _session(tmp_path)

    assert real_user_texts(session) == ["migrate the config", "we always keep YAML"]
    statements = [ref for ref in turn_evidence(session) if ref["kind"] == "user_statement"]
    assert [ref["summary"] for ref in statements] == ["migrate the config", "we always keep YAML"]


def test_interjection_evidence_can_be_read_back(tmp_path):
    session = _session(tmp_path)
    ref = next(
        ref for ref in turn_evidence(session)
        if ref["kind"] == "user_statement" and ref["summary"] == "we always keep YAML"
    )
    source = SessionEvidenceSource(SimpleNamespace(load=lambda session_id: session))

    [read] = source.load([EvidenceRef.from_dict(ref)])

    assert read.status == "available"
    assert "we always keep YAML" in read.text
