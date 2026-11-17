from __future__ import annotations

from tests.memory.helpers import Clock, ScriptedSelector, episode, make_service, write
from wright.application.memory.recall import (
    build_resident,
    prepare_turn_context,
    project_resident,
    reproject_facts,
)
from wright.domain.model.memory import GLOBAL_SPACE_ID, repo_space_id

REPO = repo_space_id("proj-a")


def test_resident_block_has_pinned_index_and_due_commitments(tmp_path):
    clock = Clock()
    service = make_service(tmp_path, clock=clock)
    pinned = write(service, "Always answer in Chinese.", space=GLOBAL_SPACE_ID, pin=True).fact
    indexed = write(service, "Run docker compose up db before pytest.", kind="constraint").fact
    archived = write(service, "CI might be flaky on Mondays.", actor="agent").fact
    service.create_commitment("Remind the user to renew the passport", space_id=GLOBAL_SPACE_ID, due="2026-11-03")
    service.create_commitment("Far away", space_id=GLOBAL_SPACE_ID, due="2027-06-01")
    view = build_resident(service, space_ids=[GLOBAL_SPACE_ID, REPO], current_space=REPO, now=clock())
    assert view.text.startswith("<MEMORY_CONTEXT>")
    assert f"[{pinned.id}]" in view.text and "Always answer in Chinese." in view.text
    assert f"[{indexed.id}]" in view.text
    assert archived.id not in view.text
    assert "1 more facts are reachable" in view.text
    assert "renew the passport" in view.text
    assert "Far away" not in view.text


def test_resident_block_only_reads_readable_spaces(tmp_path):
    clock = Clock()
    service = make_service(tmp_path, clock=clock)
    service.ensure_repo_space("proj-b")
    write(service, "Project B uses Gradle.", space=repo_space_id("proj-b"))
    view = build_resident(service, space_ids=[GLOBAL_SPACE_ID, REPO], current_space=REPO, now=clock())
    assert "Gradle" not in view.text


def test_pinned_overflow_falls_back_to_the_index(tmp_path):
    clock = Clock()
    service = make_service(tmp_path, clock=clock)
    long_text = "Detail " * 200
    first = write(service, "First pinned: " + long_text, space=GLOBAL_SPACE_ID, pin=True).fact
    second = write(service, "Second pinned: " + long_text, space=GLOBAL_SPACE_ID, pin=True, force_new=True).fact
    view = build_resident(service, space_ids=[GLOBAL_SPACE_ID], current_space=GLOBAL_SPACE_ID, now=clock(), pinned_budget=500)
    assert view.pinned_ids == (first.id,)
    assert second.id in view.indexed_ids


def test_project_resident_replaces_any_earlier_prefix():
    once = project_resident("SYSTEM", "<MEMORY_CONTEXT>\nA\n</MEMORY_CONTEXT>")
    twice = project_resident(once, "<MEMORY_CONTEXT>\nB\n</MEMORY_CONTEXT>")
    assert twice == "<MEMORY_CONTEXT>\nB\n</MEMORY_CONTEXT>\n\nSYSTEM"
    legacy = project_resident("<CORE_MEMORY>\nold\n</CORE_MEMORY>\n\nSYSTEM", "")
    assert legacy == "SYSTEM"


def test_turn_context_injects_only_what_the_selector_picked(tmp_path):
    selector = ScriptedSelector()
    service = make_service(tmp_path, selector=selector)
    chosen = write(service, "Orders API pagination uses cursors.", kind="constraint").fact
    write(service, "Unrelated note about the logo color.")
    service.save_episode(episode("ep-past", goal="add pagination to orders API", session_id="old"))
    service.save_episode(episode("ep-same", goal="pagination in this very session", session_id="now"))
    selector.facts = (chosen.id, "f-0000000000000bad")
    selector.episodes = ("ep-past", "ep-same")
    context = prepare_turn_context(
        service, "add pagination to the orders API", space_ids=[GLOBAL_SPACE_ID, REPO],
        project_id="proj-a", session_id="now",
    )
    assert "ep-same" not in selector.calls[0]["episodes"]
    assert context.selected_fact_ids == (chosen.id,)
    assert "Orders API pagination uses cursors." in context.fact_text
    assert "logo color" not in context.fact_text
    assert context.rendered_episode_ids == ("ep-past",)
    assert service.get_fact(chosen.id).use_count == 1


def test_selector_failure_injects_nothing(tmp_path):
    service = make_service(tmp_path, selector=ScriptedSelector(failed=True))
    write(service, "Orders API pagination uses cursors.")
    context = prepare_turn_context(service, "pagination", space_ids=[REPO], project_id="proj-a")
    assert context.selector_failed
    assert context.fact_text == "" and context.episode_text == ""


def test_reproject_drops_a_fact_forgotten_mid_turn(tmp_path):
    selector = ScriptedSelector()
    service = make_service(tmp_path, selector=selector)
    fact = write(service, "Orders API pagination uses cursors.").fact
    selector.facts = (fact.id,)
    context = prepare_turn_context(service, "pagination", space_ids=[REPO], project_id="proj-a")
    service.forget(fact.id)
    assert reproject_facts(service, context).fact_text == ""
