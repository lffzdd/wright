from ..memory.paths import memory_dir
from ..paths import (
    ensure_project_state,
    mcp_config_paths,
    project_id,
    project_mcp_config_path,
    project_skills_dir,
    session_dir,
    skill_directories,
    task_db_path,
    trace_dir,
    user_mcp_config_path,
    user_skills_dir,
    wright_home,
)


def test_wright_home_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    assert wright_home() == (tmp_path / "home").resolve()


def test_project_state_is_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "app"
    workspace.mkdir()
    root = ensure_project_state(workspace)
    assert workspace not in root.parents and root != workspace
    assert session_dir(workspace).is_dir()
    assert trace_dir(workspace).is_dir()
    assert task_db_path(workspace).parent == root
    assert project_id(workspace) in str(root)


def test_same_workspace_reuses_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "app"
    workspace.mkdir()
    assert project_id(workspace) == project_id(workspace / ".")


def test_memory_defaults_under_wright_home(tmp_path, monkeypatch):
    monkeypatch.delenv("WRIGHT_MEMORY_DIR", raising=False)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    assert memory_dir() == (tmp_path / "home" / "memory").resolve()


def test_skill_directories_prefer_project_over_user(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "app"
    workspace.mkdir()
    dirs = skill_directories(workspace)
    assert dirs[0] == project_skills_dir(workspace)
    assert dirs[1] == user_skills_dir()
    assert dirs[0] == (workspace / ".wright" / "skills").resolve()


def test_mcp_config_paths_user_then_project(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "app"
    workspace.mkdir()
    paths = mcp_config_paths(workspace)
    assert paths[0] == user_mcp_config_path()
    assert paths[1] == project_mcp_config_path(workspace)
    assert paths[0] == (tmp_path / "home" / "mcp.json").resolve()
    assert paths[1] == (workspace / ".wright" / "mcp.json").resolve()
