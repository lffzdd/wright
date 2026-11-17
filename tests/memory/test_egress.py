"""Egress rules (docs/memory.md): the L-cases at unit level.

Each case seeds memory, builds one outgoing tool call, and checks the verdict.
The paraphrase judge is scripted; end-to-end runs with a real model are not
part of this suite.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.memory.helpers import Clock
from wright.application.memory.dto import WriteRequest
from wright.application.memory.egress import EgressGuard
from wright.application.memory.llm_util import SideQueryResult
from wright.application.memory.service import MemoryService
from wright.core.paths import project_id
from wright.domain.model.memory import GLOBAL_SPACE_ID, repo_space_id
from wright.domain.model.session import USER_AUTHORED_SOURCES
from wright.domain.policy.memory import classify_command
from wright.infrastructure.persistence.memory import SqliteMemoryStore

USER_SOURCE = min(USER_AUTHORED_SOURCES)


@pytest.fixture
def world(tmp_path):
    repo_a = tmp_path / "company-app"
    repo_b = tmp_path / "open-source-lib"
    repo_a.mkdir()
    repo_b.mkdir()
    service = MemoryService(SqliteMemoryStore(tmp_path / "memory"), clock=Clock())
    space_a = service.ensure_repo_space(project_id(repo_a), str(repo_a)).id
    service.ensure_repo_space(project_id(repo_b), str(repo_b))

    def remember(content, space=GLOBAL_SPACE_ID, subject="user", **options):
        outcome = service.write_fact(WriteRequest(content=content, kind="fact", space_id=space, subject=subject, actor="user", **options))
        assert outcome.ok, outcome.message
        return outcome.fact

    facts = {
        "interview": remember("我最近在面试其他公司，还没告诉现在的团队"),
        "daughter": remember("女儿叫小美，今年 8 岁", markers=("小美",)),
        "therapy": remember("我每周去看心理医生"),
        "name": remember("我叫李明", markers=("李明",)),
        "ray": remember("My first name is Ray", markers=("Ray",)),
        "rust": remember("用户喜欢 Rust"),
        "host": remember("内部数据库地址是 db.corp.internal", space=space_a, subject="project"),
        "codename": remember("内部项目代号叫北极星", space=space_a, subject="project", markers=("北极星",)),
        "tests_first": remember("我喜欢先写测试", space=space_a, subject="user"),
        "db_rule": remember("测试前必须先启动数据库容器 docker compose up db", space=space_a, subject="project"),
    }
    return SimpleNamespace(service=service, repo_a=repo_a, repo_b=repo_b, facts=facts)


def session(*user_texts):
    records = [SimpleNamespace(source=USER_SOURCE, message={"role": "user", "content": text}) for text in user_texts]
    return SimpleNamespace(active_turn_start_message_index=0, message_records=records)


def judge_returning(*fact_ids, failed=False):
    calls = []

    def judge(system, user):
        calls.append(user)
        if failed:
            return SideQueryResult("", True, "timeout", None, 0.0, "m")
        return SideQueryResult(json.dumps({"reveals": list(fact_ids)}), False, "", None, 0.0, "m")

    judge.calls = calls
    return judge


def shell(guard, root, command, *user_texts):
    return guard.inspect_call(
        tool_name="shell", operations=["shell"], arguments={"command": command},
        write_paths=[], project_root=str(root), session=session(*user_texts),
    )


def write_file(guard, root, path, content, *user_texts):
    return guard.inspect_call(
        tool_name="write", operations=["file_write"], arguments={"path": str(path), "content": content},
        write_paths=[str(path)], project_root=str(root), session=session(*user_texts),
    )


def web_search(guard, root, query, *user_texts):
    return guard.inspect_call(
        tool_name="web_search", operations=["network_read"], arguments={"query": query},
        write_paths=[], project_root=str(root), session=session(*user_texts),
    )


# --- sink classification ------------------------------------------------

@pytest.mark.parametrize(("command", "sink"), [
    ('git commit -m "fix"', "E2"),
    ("git -C repo push origin main", "E2"),
    ('gh pr create --title t --body b', "E2"),
    ("npm publish", "E2"),
    ("curl -X POST https://example.com -d x", "E3"),
    ("echo hi > notes.txt", "E1"),
    ("cat <<'EOF' > a.py", "E1"),
    ("git status", "E0"),
    ("ls -la && pytest -q", "E0"),
    ("grep -n '2>&1' file", "E0"),
])
def test_commands_are_classified_by_where_their_text_goes(command, sink):
    assert classify_command(command).sink == sink


# --- A. direct leaks ----------------------------------------------------

def test_L01_personal_fact_in_a_commit_is_held(world):
    verdict = shell(EgressGuard(world.service), world.repo_a, 'git commit -m "我最近在面试其他公司，所以先交接"')
    assert verdict.action == "ask"
    assert verdict.hits[0].fact_id == world.facts["interview"].id


def test_L02_paraphrase_is_caught_by_the_judge_on_E2(world):
    judge = judge_returning(world.facts["interview"].id)
    verdict = shell(EgressGuard(world.service, judge=judge), world.repo_a, 'git commit -m "最近在看新机会，先把这块交接了"')
    assert verdict.action == "ask"
    assert verdict.hits[0].reason == "judge"
    assert judge.calls and "面试其他公司" in judge.calls[0]


def test_L02_judge_is_not_spent_on_E1_or_E3(world):
    judge = judge_returning(world.facts["interview"].id)
    guard = EgressGuard(world.service, judge=judge)
    assert write_file(guard, world.repo_a, world.repo_a / "a.py", "x = 1").allowed
    assert web_search(guard, world.repo_a, "python list sort").allowed
    assert judge.calls == []


def test_L02_judge_failure_holds_the_commit(world):
    verdict = shell(EgressGuard(world.service, judge=judge_returning(failed=True)), world.repo_a, 'git commit -m "refactor"')
    assert verdict.action == "ask"
    assert verdict.hits[0].reason == "judge_unavailable"


def test_L03_third_party_name_in_code_is_held(world):
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "game.py", "# 给小美做的乘法游戏\n", "给我女儿写个练乘法的小游戏")
    assert verdict.action == "ask"
    assert verdict.hits[0].fact_id == world.facts["daughter"].id


def test_L04_user_typed_it_this_turn_so_it_may_go(world):
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "game.py", "TITLE = '给小美的乘法练习'\n", "标题写上'给小美的乘法练习'")
    assert verdict.allowed


def test_L05_permission_does_not_carry_to_the_next_turn(world):
    guard = EgressGuard(world.service)
    assert shell(guard, world.repo_a, 'gh pr create --body "给小美的游戏"', "PR 里写上给小美的游戏").allowed
    assert shell(guard, world.repo_a, 'gh pr create --body "给小美的游戏 v2"', "再开一个 PR").action == "ask"


# --- B. team rules files -------------------------------------------------

def test_L06_personal_preference_does_not_enter_agents_md(world):
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "AGENTS.md",
                         "## Build\nuv sync\n\n我喜欢先写测试\n", "更新一下 AGENTS.md，补上构建命令")
    assert verdict.action == "ask"
    assert any(hit.fact_id == world.facts["tests_first"].id for hit in verdict.hits)


def test_L07_user_asked_for_the_team_rule(world):
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "AGENTS.md",
                         "## Workflow\n先写测试，再写实现。\n", "把先写测试加进团队规范")
    assert verdict.allowed


def test_L08_unrequested_rules_file_write_is_held(world):
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "AGENTS.md", "## Build\nuv sync\n", "修一下登录 bug")
    assert verdict.action == "ask"
    assert verdict.hits[0].level == "shared_rules"


# --- C. across repositories ----------------------------------------------

def test_L09_internal_host_does_not_reach_another_repo(world):
    guard = EgressGuard(world.service)
    readme = world.repo_b / "README.md"
    verdict = write_file(guard, world.repo_b, readme, "Example: DATABASE_URL=postgres://db.corp.internal/app\n")
    assert verdict.action == "ask"
    assert verdict.hits[0].level == "foreign_project"
    assert write_file(guard, world.repo_a, world.repo_a / "README.md", "DB host: db.corp.internal\n").allowed


def test_L10_internal_host_stays_out_of_searches_from_another_repo(world):
    verdict = web_search(EgressGuard(world.service), world.repo_b, "connection refused db.corp.internal 5432")
    assert verdict.action == "ask"


def test_L11_codename_stays_out_of_another_repos_commits(world):
    verdict = shell(EgressGuard(world.service), world.repo_b, 'git commit -m "port 北极星 retry logic"')
    assert verdict.action == "ask"


# --- D. third-party services ---------------------------------------------

def test_L12_health_detail_stays_out_of_mail(world):
    verdict = shell(EgressGuard(world.service), world.repo_a, 'mail -s "请假" boss@example.com <<< "我每周去看心理医生，周三请假"')
    assert verdict.action == "ask"
    assert verdict.hits[0].level == "sensitive"


def test_L13_real_name_stays_out_of_search_queries(world):
    assert web_search(EgressGuard(world.service), world.repo_a, "李明 python asyncio timeout").action == "ask"


# --- F. failure handling --------------------------------------------------

def test_L17_L18_a_broken_check_holds_the_call(world, monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(world.service, "protected_facts", boom)
    verdict = shell(EgressGuard(world.service), world.repo_a, 'git commit -m "fix"')
    assert verdict.action == "ask"
    assert verdict.hits[0].reason == "check_failed"


# --- G. credentials --------------------------------------------------------

def test_L22_L23_credentials_are_refused_even_when_the_user_typed_them(world):
    token = "ghp_" + "a" * 36
    verdict = write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "config.py",
                         f"TOKEN = '{token}'\n", f"把这个 key 写进 config.py: {token}")
    assert verdict.action == "reject"


# --- H. false positives ------------------------------------------------------

def test_L24_own_project_facts_may_go_into_its_pr(world):
    verdict = shell(EgressGuard(world.service), world.repo_a, 'gh pr create --body "测试前必须先启动数据库容器 docker compose up db"')
    assert verdict.allowed


def test_L25_a_name_that_is_also_a_library_does_not_block_code(world):
    assert write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "train.py", "import ray\nray.init()\n").allowed


def test_L26_liking_a_language_does_not_block_that_language(world):
    assert write_file(EgressGuard(world.service), world.repo_a, world.repo_a / "lib.rs", "// Rust code\nfn main() {}\n").allowed


def test_L27_private_notes_outside_the_project_are_local(world, tmp_path):
    notes = tmp_path / "scratch" / "notes.md"
    assert write_file(EgressGuard(world.service), world.repo_a, notes, "我最近在面试其他公司").allowed


def test_reads_and_memory_tools_are_never_checked(world):
    guard = EgressGuard(world.service)
    assert guard.inspect_call(tool_name="read", operations=["file_read"], arguments={"path": "我最近在面试其他公司"},
                              write_paths=[], project_root=str(world.repo_a), session=None).allowed
    assert guard.inspect_call(tool_name="memory_write", operations=["persistent_write"], arguments={"content": "李明"},
                              write_paths=[], project_root=str(world.repo_a), session=None).allowed


def test_shareable_fact_may_leave(world):
    world.service.update_fact(world.facts["name"].id, shareable=True, actor="user")
    assert web_search(EgressGuard(world.service), world.repo_a, "李明 github").allowed


def test_space_ids_follow_project_roots(world):
    assert world.facts["host"].space_id == repo_space_id(project_id(world.repo_a))
