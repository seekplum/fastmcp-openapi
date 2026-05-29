from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from bin import poe_tasks


def test_generate_poe_config_allows_poe_to_expose_extra_args_via_sys_argv() -> None:
    config = poe_tasks.generate_poe_config()

    test_task = config["tasks"]["test"]

    assert test_task["script"] == "bin.poe_tasks:test"


def test_test_uses_default_pytest_args_when_no_extra_args(monkeypatch: Any) -> None:
    calls: list[Sequence[str]] = []

    def fake_run(args: Sequence[str], **kwargs: Any) -> None:
        del kwargs
        calls.append(args)

    monkeypatch.setattr(poe_tasks, "_run", fake_run)
    monkeypatch.setattr(poe_tasks.sys, "argv", ["test"])

    poe_tasks.test()

    assert calls == [
        (
            "pytest",
            "-svv",
            "--cov=src",
            "--cov-fail-under=50",
            "--cov-report=term-missing",
            "tests",
        )
    ]


def test_test_uses_only_extra_pytest_args_when_provided(monkeypatch: Any) -> None:
    calls: list[Sequence[str]] = []

    def fake_run(args: Sequence[str], **kwargs: Any) -> None:
        del kwargs
        calls.append(args)

    monkeypatch.setattr(poe_tasks, "_run", fake_run)
    monkeypatch.setattr(poe_tasks.sys, "argv", ["test", "--cov=./", "--cov-report=xml", "-ra"])

    poe_tasks.test()

    assert calls == [("pytest", "--cov=./", "--cov-report=xml", "-ra")]
