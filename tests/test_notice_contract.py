"""The same notice cases drive the Python snapshot and TypeScript live tests."""

import json
from pathlib import Path

import pytest

from wright.application.session.events import notice_display, notice_text

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "web/src/notice-contract.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_notice_contract(case):
    code, params = notice_display(case["type"], case["payload"])
    assert {
        "text": notice_text(case["type"], case["payload"]),
        "code": code,
        "params": params,
    } == case["expected"]
