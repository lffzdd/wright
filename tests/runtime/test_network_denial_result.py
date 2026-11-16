"""Hosts the egress proxy refused are reported with the command result."""

from pathlib import Path

from wright.application.command.execution import CommandExecution
from wright.application.execution.identity import bind_identity
from wright.domain.model.session import Session


class Process:
    returncode = 6

    def __init__(self):
        self._chunks = [b"curl: (56) CONNECT tunnel failed, response 403\n", None]

    def read_output(self, max_bytes):
        return self._chunks.pop(0) if self._chunks else None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def group_alive(self):
        return False

    def terminate(self, *, grace_seconds=2.0):
        return True

    def cwd_result(self):
        return None

    def network_denials(self):
        return [{"host": "pypi.org", "port": 443, "reason": "not allowed"}]


class Execution:
    grant = None

    def __init__(self):
        self.ended = False

    def start_shell(self, command):
        return Process()

    def end_interaction(self):
        self.ended = True

    def cwd(self):
        return "/work"

    def display_path(self, path):
        return "."


def test_shell_result_lists_refused_hosts_and_ends_questions(tmp_path: Path):
    session = Session.create("denials", tmp_path)
    commands = CommandExecution(session, bind_identity(session))
    execution = Execution()
    outcome = commands.execute(command="pip install x", timeout=5, run_in_background=False, execution=execution,
                               allow_background=True, emit_output=None, is_cancelled=lambda: False,
                               root_turn_id="t", run_id="r", access_scope=None, set_cwd=None)
    assert not outcome.ok
    assert outcome.data["network_denials"] == [{"host": "pypi.org", "port": 443, "reason": "not allowed"}]
    assert "pypi.org:443" in outcome.data["message"]
    assert execution.ended
    commands.close()
