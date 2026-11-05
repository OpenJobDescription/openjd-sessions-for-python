# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Brittle sessions: once an action ends FAILED, CANCELED or TIMEOUT, the
Session only accepts environment exits and cleanup, and it never returns to
READY, whatever a later exit_environment passes as keep_session_running.
"""

import time
import uuid
from typing import Optional
from unittest.mock import patch

import pytest

from openjd.model import ParameterValue
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import ArgString as ArgString_2023_09
from openjd.model.v2023_09 import CommandString as CommandString_2023_09
from openjd.model.v2023_09 import Environment as Environment_2023_09
from openjd.model.v2023_09 import EnvironmentActions as EnvironmentActions_2023_09
from openjd.model.v2023_09 import EnvironmentScript as EnvironmentScript_2023_09
from openjd.model.v2023_09 import (
    EnvironmentVariableValueString as EnvironmentVariableValueString_2023_09,
)
from openjd.model.v2023_09 import StepActions as StepActions_2023_09
from openjd.model.v2023_09 import StepScript as StepScript_2023_09
from openjd.sessions import ActionState, Session, SessionState

from .conftest import serial_process


def _action(python_exe: str, code: str, timeout: Optional[int] = None) -> Action_2023_09:
    return Action_2023_09(
        command=CommandString_2023_09(python_exe),
        args=[ArgString_2023_09("-c"), ArgString_2023_09(code)],
        timeout=timeout,
    )


def _step(action: Action_2023_09) -> StepScript_2023_09:
    return StepScript_2023_09(actions=StepActions_2023_09(onRun=action))


def _env(
    name: str,
    on_enter: Optional[Action_2023_09] = None,
    on_exit: Optional[Action_2023_09] = None,
) -> Environment_2023_09:
    if on_enter is None and on_exit is None:
        # An environment needs a script or variables.
        return Environment_2023_09(
            name=name,
            variables={"BRITTLE_TEST": EnvironmentVariableValueString_2023_09("1")},
        )
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(onEnter=on_enter, onExit=on_exit)
        ),
    )


def _wait(session: Session) -> None:
    deadline = time.monotonic() + 20
    while session.state in (SessionState.RUNNING, SessionState.CANCELING):
        assert time.monotonic() < deadline, "action did not finish"
        time.sleep(0.05)


def _no_params() -> dict[str, ParameterValue]:
    return dict[str, ParameterValue]()


def _assert_refuses_new_actions(session: Session, python_exe: str) -> None:
    with pytest.raises(RuntimeError, match="READY state"):
        session.run_task(
            step_script=_step(_action(python_exe, "print('again')")),
            task_parameter_values=_no_params(),
        )
    with pytest.raises(RuntimeError, match="READY state"):
        session.enter_environment(environment=_env("New", on_enter=_action(python_exe, "pass")))
    with pytest.raises(RuntimeError, match="READY state"):
        session.enter_environment(environment=_env("NoAction"))
    with pytest.raises(RuntimeError, match="READY state"):
        session.run_subprocess(command=python_exe, args=["-c", "pass"])


def _run_ending_task(session: Session, python_exe: str, ending: ActionState) -> None:
    if ending == ActionState.FAILED:
        action = _action(python_exe, "import sys; sys.exit(1)")
    elif ending == ActionState.CANCELED:
        action = _action(python_exe, "import time; time.sleep(30)")
    else:
        action = _action(python_exe, "import time; time.sleep(30)", timeout=1)
    session.run_task(step_script=_step(action), task_parameter_values=_no_params())
    if ending == ActionState.CANCELED:
        time.sleep(0.5)
        session.cancel_action()
    _wait(session)
    assert session.action_status is not None
    assert session.action_status.state == ending
    assert session.state == SessionState.READY_ENDING


@serial_process
class TestBrittleSession:
    @pytest.mark.parametrize(
        "ending", [ActionState.FAILED, ActionState.CANCELED, ActionState.TIMEOUT]
    )
    @pytest.mark.parametrize("keep_session_running", [True, False])
    @pytest.mark.parametrize("with_on_exit", [True, False])
    def test_task_ending_then_exit(
        self,
        python_exe: str,
        ending: ActionState,
        keep_session_running: bool,
        with_on_exit: bool,
    ) -> None:
        # GIVEN
        on_exit = _action(python_exe, "print('exited')") if with_on_exit else None
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(environment=_env("Env", on_exit=on_exit))
            _wait(session)
            assert session.state == SessionState.READY
            _run_ending_task(session, python_exe, ending)
            _assert_refuses_new_actions(session, python_exe)

            # WHEN
            session.exit_environment(identifier=env_id, keep_session_running=keep_session_running)
            _wait(session)

            # THEN
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_failed_on_enter_then_exit_keep_running(self, python_exe: str) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(
                environment=_env("Env", on_enter=_action(python_exe, "import sys; sys.exit(3)"))
            )
            _wait(session)
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=env_id, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_failed_on_exit_then_exit_outer_keep_running(self, python_exe: str) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            outer = session.enter_environment(environment=_env("Outer"))
            _wait(session)
            inner = session.enter_environment(
                environment=_env("Inner", on_exit=_action(python_exe, "import sys; sys.exit(4)"))
            )
            _wait(session)
            session.exit_environment(identifier=inner, keep_session_running=True)
            _wait(session)
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=outer, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_unresolvable_format_string_then_exit_keep_running(self, python_exe: str) -> None:
        # A format string naming an undefined parameter fails the action in the
        # runner, before its subprocess starts.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(environment=_env("Env"))
            _wait(session)
            session.run_task(
                step_script=_step(_action(python_exe, "print('{{ Task.Param.Missing }}')")),
                task_parameter_values=_no_params(),
            )
            _wait(session)
            assert session.action_status is not None
            assert session.action_status.state == ActionState.FAILED
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=env_id, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_failure_before_start_then_exit_keep_running(self, python_exe: str) -> None:
        # A failure while the Session prepares the action, before any runner
        # exists, goes through _fail_action_before_start.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(environment=_env("Env"))
            _wait(session)
            with patch.object(
                session, "_materialize_path_mapping", side_effect=RuntimeError("boom")
            ):
                session.run_task(
                    step_script=_step(_action(python_exe, "print('never runs')")),
                    task_parameter_values=_no_params(),
                )
            assert session.action_status is not None
            assert session.action_status.state == ActionState.FAILED
            assert session.action_status.fail_message == "boom"
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=env_id, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_failed_subprocess_then_exit_keep_running(self, python_exe: str) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(environment=_env("Env"))
            _wait(session)
            session.run_subprocess(command=python_exe, args=["-c", "import sys; sys.exit(5)"])
            _wait(session)
            assert session.action_status is not None
            assert session.action_status.state == ActionState.FAILED
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=env_id, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    def test_cancel_marked_failed_then_exit_keep_running(self, python_exe: str) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(environment=_env("Env"))
            _wait(session)
            session.run_task(
                step_script=_step(_action(python_exe, "import time; time.sleep(30)")),
                task_parameter_values=_no_params(),
            )
            time.sleep(0.5)
            session.cancel_action(mark_action_failed=True)
            _wait(session)
            assert session.action_status is not None
            assert session.action_status.state == ActionState.FAILED
            assert session.state == SessionState.READY_ENDING

            session.exit_environment(identifier=env_id, keep_session_running=True)
            _wait(session)

            assert session.state == SessionState.READY_ENDING
            _assert_refuses_new_actions(session, python_exe)

    @pytest.mark.parametrize(
        "keep_session_running, expected",
        [(True, SessionState.READY), (False, SessionState.READY_ENDING)],
    )
    def test_successful_actions_keep_running_controls_reopening(
        self, python_exe: str, keep_session_running: bool, expected: SessionState
    ) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values=_no_params()) as session:
            env_id = session.enter_environment(
                environment=_env("Env", on_exit=_action(python_exe, "print('bye')"))
            )
            _wait(session)
            session.run_task(
                step_script=_step(_action(python_exe, "print('ok')")),
                task_parameter_values=_no_params(),
            )
            _wait(session)
            assert session.state == SessionState.READY

            session.exit_environment(identifier=env_id, keep_session_running=keep_session_running)
            _wait(session)

            assert session.state == expected
            if expected == SessionState.READY:
                session.run_task(
                    step_script=_step(_action(python_exe, "print('again')")),
                    task_parameter_values=_no_params(),
                )
                _wait(session)
                assert session.action_status is not None
                assert session.action_status.state == ActionState.SUCCESS
                assert session.state == SessionState.READY
            else:
                _assert_refuses_new_actions(session, python_exe)
