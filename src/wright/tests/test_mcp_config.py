import json
from pathlib import Path

from ..tools.mcp_client import load_mcp_config, load_mcp_configs


def test_missing_mcp_file_is_empty(tmp_path: Path):
    assert load_mcp_config(tmp_path / "mcp.json") == []


def test_load_mcp_config_parses_stdio(tmp_path: Path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo": {
                        "command": "uv",
                        "args": ["run", "python", "server.py"],
                        "env": {"FOO": "1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    configs = load_mcp_config(path)
    assert len(configs) == 1
    assert configs[0].name == "echo"
    assert configs[0].transport == "stdio"
    assert configs[0].command == "uv"
    assert configs[0].args == ["run", "python", "server.py"]
    assert configs[0].env == {"FOO": "1"}


def test_later_config_overrides_same_server_name(tmp_path: Path):
    user = tmp_path / "user.json"
    project = tmp_path / "project.json"
    user.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "user-bin"},
                    "only-user": {"command": "user-only"},
                }
            }
        ),
        encoding="utf-8",
    )
    project.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "project-bin", "args": ["--prod"]},
                }
            }
        ),
        encoding="utf-8",
    )
    configs = {item.name: item for item in load_mcp_configs([user, project])}
    assert configs["shared"].command == "project-bin"
    assert configs["shared"].args == ["--prod"]
    assert configs["only-user"].command == "user-only"
    assert set(configs) == {"shared", "only-user"}
