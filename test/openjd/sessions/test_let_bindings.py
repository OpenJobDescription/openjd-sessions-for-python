# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""End-to-end tests for EXPR ``let`` binding resolution (RFC 0007) in the
pure-Python (v0) Session.

These prove that ``let`` bindings declared on a step script / environment script
are evaluated at run time and made available to the action's ``{{ }}``
expressions. The RHS of each binding is an EXPR expression evaluated by the Rust
engine through the model's bindings; chained bindings (one referencing an
earlier one) resolve in order.
"""

import time
from pathlib import Path

from openjd.model import parse_model
from openjd.model.v2023_09 import (
    Environment as Environment_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import Session, SessionState

_EXTS = ["EXPR"]

# Inline python that writes argv[2] to the file at argv[1].
_WRITE = "import sys\nopen(sys.argv[1], 'w').write(sys.argv[2])"


def _wait_until_ready(session: Session) -> None:
    deadline = time.monotonic() + 30
    while session.state == SessionState.RUNNING:
        if time.monotonic() > deadline:  # pragma: no cover
            raise AssertionError("Timed out waiting for action to finish")
        time.sleep(0.05)


def _onrun(python_exe: str, outfile: Path, content: str) -> dict:
    return {"command": python_exe, "args": ["-c", _WRITE, outfile.as_posix(), content]}


def _step_with_let(python_exe: str, out: Path, *, let: list, content: str) -> StepScript_2023_09:
    return parse_model(
        model=StepScript_2023_09,
        obj={"let": let, "actions": {"onRun": _onrun(python_exe, out / "out.txt", content)}},
        supported_extensions=_EXTS,
    )


class TestStepLetRuntime:
    def test_literal_let_resolves(self, session_id: str, python_exe: str, tmp_path: Path) -> None:
        # GIVEN a step whose let binding computes a value referenced by onRun.
        step = _step_with_let(
            python_exe, tmp_path, let=["doubled = 21 * 2"], content="{{ doubled }}"
        )

        # WHEN the task runs
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.run_task(step_script=step, task_parameter_values={})
            _wait_until_ready(session)

        # THEN the let value resolved at run time
        assert (tmp_path / "out.txt").read_text() == "42"

    def test_chained_let_resolves_in_order(
        self, session_id: str, python_exe: str, tmp_path: Path
    ) -> None:
        # A later binding references an earlier one (RFC 0007 chaining).
        step = _step_with_let(python_exe, tmp_path, let=["a = 2", "b = a + 3"], content="{{ b }}")
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.run_task(step_script=step, task_parameter_values={})
            _wait_until_ready(session)
        assert (tmp_path / "out.txt").read_text() == "5"


class TestEnvLetRuntime:
    def test_env_script_let_resolves_in_on_enter(
        self, session_id: str, python_exe: str, tmp_path: Path
    ) -> None:
        env = parse_model(
            model=Environment_2023_09,
            obj={
                "name": "E",
                "script": {
                    "let": ["greeting = 'hello'"],
                    "actions": {
                        "onEnter": _onrun(python_exe, tmp_path / "out.txt", "{{ greeting }}")
                    },
                },
            },
            supported_extensions=_EXTS,
        )
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=env)
            _wait_until_ready(session)
        assert (tmp_path / "out.txt").read_text() == "hello"
