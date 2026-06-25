# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""End-to-end tests for the RFC 0008 WRAP_ACTIONS runtime in the pure-Python
(v0) Session.

These exercise the three wrap-hook dispatch sites (onWrapEnvEnter /
onWrapTaskRun / onWrapEnvExit). Expression resolution of the seeded
``WrappedAction.*`` / ``WrappedEnv.Name`` / ``WrappedStep.Name`` symbols is
handled by the Rust EXPR engine through the model's existing bindings.
"""

import time
from pathlib import Path

from openjd.model import parse_model
from openjd.model.v2023_09 import (
    Environment as Environment_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import ActionState, Session, SessionState

_EXTS = ["EXPR", "WRAP_ACTIONS"]

# Inline python that writes argv[2] to the file at argv[1].
_WRITE = "import sys\nopen(sys.argv[1], 'w').write(sys.argv[2])"


def _wait_until_ready(session: Session) -> None:
    """Block until the current action finishes."""
    deadline = time.monotonic() + 30
    while session.state == SessionState.RUNNING:
        if time.monotonic() > deadline:  # pragma: no cover
            raise AssertionError("Timed out waiting for action to finish")
        time.sleep(0.05)


def _action(python_exe: str, outfile: Path, content: str) -> dict:
    return {
        "command": python_exe,
        "args": ["-c", _WRITE, outfile.as_posix(), content],
    }


def _wrap_environment(python_exe: str, out: Path) -> Environment_2023_09:
    """A wrap environment defining its own onEnter/onExit plus all three wrap
    hooks. The wrap hooks reference the seeded WrappedAction.* / WrappedEnv.* /
    WrappedStep.* symbols so we can prove they resolved through EXPR.
    """
    env_dict = {
        "name": "WrapEnv",
        "script": {
            "actions": {
                "onEnter": _action(python_exe, out / "wrap_own_enter.txt", "own-enter"),
                "onExit": _action(python_exe, out / "wrap_own_exit.txt", "own-exit"),
                "onWrapEnvEnter": _action(
                    python_exe,
                    out / "wrap_enter.txt",
                    "cmd={{ WrappedAction.Command }} env={{ WrappedEnv.Name }} "
                    "timeout={{ WrappedAction.Timeout }}",
                ),
                "onWrapTaskRun": _action(
                    python_exe,
                    out / "wrap_task.txt",
                    "cmd={{ WrappedAction.Command }} step={{ WrappedStep.Name }}",
                ),
                "onWrapEnvExit": _action(
                    python_exe,
                    out / "wrap_exit.txt",
                    "env={{ WrappedEnv.Name }}",
                ),
            }
        },
    }
    return parse_model(model=Environment_2023_09, obj=env_dict, supported_extensions=_EXTS)


def _inner_environment(python_exe: str, out: Path) -> Environment_2023_09:
    env_dict = {
        "name": "InnerEnv",
        "script": {
            "actions": {
                "onEnter": _action(python_exe, out / "inner_enter.txt", "inner-enter"),
                "onExit": _action(python_exe, out / "inner_exit.txt", "inner-exit"),
            }
        },
    }
    return parse_model(model=Environment_2023_09, obj=env_dict, supported_extensions=_EXTS)


def _step_script(python_exe: str, out: Path) -> StepScript_2023_09:
    step_dict = {
        "actions": {"onRun": _action(python_exe, out / "task_run.txt", "task-run")},
    }
    return parse_model(model=StepScript_2023_09, obj=step_dict, supported_extensions=_EXTS)


class TestWrapActions:
    def test_wrap_dispatch_end_to_end(
        self, session_id: str, python_exe: str, tmp_path: Path
    ) -> None:
        # GIVEN
        wrap_env = _wrap_environment(python_exe, tmp_path)
        inner_env = _inner_environment(python_exe, tmp_path)
        step = _step_script(python_exe, tmp_path)

        with Session(session_id=session_id, job_parameter_values={}) as session:
            # WHEN: enter the wrap env. Its own onEnter runs (not self-wrapped).
            session.enter_environment(environment=wrap_env)
            _wait_until_ready(session)
            assert session.state == SessionState.READY

            # WHEN: enter the inner env. onWrapEnvEnter runs in place of onEnter.
            session.enter_environment(environment=inner_env)
            _wait_until_ready(session)
            assert session.state == SessionState.READY

            # WHEN: run a task. onWrapTaskRun runs in place of onRun.
            session.run_task(step_script=step, task_parameter_values={}, step_name="MyStep")
            _wait_until_ready(session)
            assert session.state == SessionState.READY

            # WHEN: exit the inner env. onWrapEnvExit runs in place of onExit.
            inner_id = session.environments_entered[-1]
            session.exit_environment(identifier=inner_id)
            _wait_until_ready(session)

            # WHEN: exit the wrap env. Its own onExit runs (not self-wrapped).
            wrap_id = session.environments_entered[-1]
            session.exit_environment(identifier=wrap_id)
            _wait_until_ready(session)

            # THEN: the wrap env's own lifecycle actions ran (never self-wrapped).
            assert (tmp_path / "wrap_own_enter.txt").read_text() == "own-enter"
            assert (tmp_path / "wrap_own_exit.txt").read_text() == "own-exit"

            # THEN: the inner env/task lifecycle actions were REPLACED by the
            # wrap actions — the inner ones never ran.
            assert not (tmp_path / "inner_enter.txt").exists()
            assert not (tmp_path / "task_run.txt").exists()
            assert not (tmp_path / "inner_exit.txt").exists()

            # THEN: the wrap actions ran, and the seeded WrappedAction.* /
            # WrappedEnv.Name / WrappedStep.Name resolved through EXPR.
            enter_out = (tmp_path / "wrap_enter.txt").read_text()
            assert f"cmd={python_exe}" in enter_out
            assert "env=InnerEnv" in enter_out
            assert "timeout=0" in enter_out

            task_out = (tmp_path / "wrap_task.txt").read_text()
            assert f"cmd={python_exe}" in task_out
            assert "step=MyStep" in task_out

            assert (tmp_path / "wrap_exit.txt").read_text() == "env=InnerEnv"

    def test_no_wrap_when_no_wrap_env(
        self, session_id: str, python_exe: str, tmp_path: Path
    ) -> None:
        # GIVEN: only a plain (non-wrap) environment and a task.
        inner_env = _inner_environment(python_exe, tmp_path)
        step = _step_script(python_exe, tmp_path)

        with Session(session_id=session_id, job_parameter_values={}) as session:
            # WHEN
            session.enter_environment(environment=inner_env)
            _wait_until_ready(session)
            session.run_task(step_script=step, task_parameter_values={})
            _wait_until_ready(session)
            inner_id = session.environments_entered[-1]
            session.exit_environment(identifier=inner_id)
            _wait_until_ready(session)

            # THEN: with no active wrap env, the ordinary actions run unchanged.
            assert (tmp_path / "inner_enter.txt").read_text() == "inner-enter"
            assert (tmp_path / "task_run.txt").read_text() == "task-run"
            assert (tmp_path / "inner_exit.txt").read_text() == "inner-exit"
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
