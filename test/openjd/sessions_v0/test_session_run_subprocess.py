# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import time
import uuid
from unittest.mock import MagicMock

import pytest

from openjd.model import ParameterValue
from openjd.model.v2023_09 import Environment as Environment_2023_09
from openjd.model.v2023_09 import (
    EnvironmentVariableValueString as EnvironmentVariableValueString_2023_09,
)
from openjd.sessions import (
    ActionState,
    ActionStatus,
    Session,
    SessionState,
)
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._session_user import PosixSessionUser, WindowsSessionUser

from .conftest import (
    serial_process,
    has_posix_target_user,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)


@serial_process
class TestRunSubprocess:
    """Tests for the Session.run_subprocess method."""

    def test_basic_execution(self, python_exe: str, caplog: pytest.LogCaptureFixture) -> None:
        """Test successful subprocess execution with simple command."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('Hello from subprocess')"],
            )

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status is not None
            assert session.action_status.state == ActionState.RUNNING

            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "Hello from subprocess" in caplog.messages

    def test_command_with_arguments(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test command with multiple arguments."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import sys; print(' '.join(sys.argv[1:]))", "arg1", "arg2", "arg3"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert "arg1 arg2 arg3" in caplog.messages

    def test_special_characters_in_arguments(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that special characters in arguments are passed through correctly."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN - Use special characters
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('$VAR %PATH% @special')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - The literal string should be printed
            assert session.state == SessionState.READY
            assert "$VAR %PATH% @special" in caplog.messages

    def test_timeout_completes_before_timeout(self, python_exe: str) -> None:
        """Test subprocess that completes before timeout."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import time; time.sleep(0.1); print('done')"],
                timeout=5,  # 5 seconds timeout, but completes in 0.1 seconds
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

    def test_timeout_exceeds_timeout(self, python_exe: str) -> None:
        """Test subprocess that exceeds timeout and is terminated."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import time; time.sleep(10)"],
                timeout=1,  # 1 second timeout
            )

            # Wait for timeout
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status is not None
            assert session.action_status.state == ActionState.TIMEOUT

    def test_no_timeout(self, python_exe: str) -> None:
        """Test subprocess with no timeout runs until completion."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import time; time.sleep(0.2); print('completed')"],
                timeout=None,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

    def test_env_vars_with_use_session_env_vars_true(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test environment variables with use_session_env_vars=True."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        session_env_vars = {"SESSION_VAR": "session_value"}
        subprocess_env_vars = {"SUBPROCESS_VAR": "subprocess_value"}

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            os_env_vars=session_env_vars,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    'import os; print(f\'{os.environ.get("SESSION_VAR", "MISSING")} {os.environ.get("SUBPROCESS_VAR", "MISSING")}\')',
                ],
                os_env_vars=subprocess_env_vars,
                use_session_env_vars=True,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert "session_value subprocess_value" in caplog.messages

    def test_env_vars_with_use_session_env_vars_false(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that entered environment variables are excluded when use_session_env_vars=False."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        subprocess_env_vars = {"SUBPROCESS_VAR": "subprocess_value"}

        # Create an environment with a variable
        environment = Environment_2023_09(
            name="TestEnv",
            variables={
                "ENV_VAR": EnvironmentVariableValueString_2023_09("env_value"),
            },
        )

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # Enter the environment
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    'import os; print(f\'{os.environ.get("ENV_VAR", "MISSING")} {os.environ.get("SUBPROCESS_VAR", "MISSING")}\')',
                ],
                os_env_vars=subprocess_env_vars,
                use_session_env_vars=False,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - ENV_VAR should be MISSING because use_session_env_vars=False
            assert session.state == SessionState.READY
            assert "MISSING subprocess_value" in caplog.messages

    def test_env_vars_precedence(self, python_exe: str, caplog: pytest.LogCaptureFixture) -> None:
        """Test environment variable precedence (os_env_vars overrides)."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        session_env_vars = {"OVERRIDE_VAR": "session_value"}
        subprocess_env_vars = {"OVERRIDE_VAR": "subprocess_value"}

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            os_env_vars=session_env_vars,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "import os; print(os.environ.get('OVERRIDE_VAR', 'MISSING'))",
                ],
                os_env_vars=subprocess_env_vars,
                use_session_env_vars=True,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - subprocess_value should override session_value
            assert session.state == SessionState.READY
            assert "subprocess_value" in caplog.messages

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(state, id=state.value)
            for state in SessionState
            if state != SessionState.READY
        ],
    )
    def test_state_validation_not_ready(self, state: SessionState, python_exe: str) -> None:
        """Test calling from non-READY state raises RuntimeError."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session._state = state

            # THEN
            with pytest.raises(RuntimeError, match="Session must be in the READY state"):
                session.run_subprocess(command=python_exe, args=["-c", "print('test')"])

    def test_state_validation_ready_succeeds(self, python_exe: str) -> None:
        """Test calling from READY state succeeds."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            assert session.state == SessionState.READY

            # WHEN
            session.run_subprocess(command=python_exe, args=["-c", "print('test')"])

            # THEN
            assert session.state == SessionState.RUNNING

    def test_invalid_timeout_zero(self, python_exe: str) -> None:
        """Test with invalid timeout (zero)."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN/THEN
            with pytest.raises(ValueError, match="timeout must be a positive integer"):
                session.run_subprocess(
                    command=python_exe,
                    args=["-c", "print('test')"],
                    timeout=0,
                )

    def test_invalid_timeout_negative(self, python_exe: str) -> None:
        """Test with invalid timeout (negative)."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN/THEN
            with pytest.raises(ValueError, match="timeout must be a positive integer"):
                session.run_subprocess(
                    command=python_exe,
                    args=["-c", "print('test')"],
                    timeout=-1,
                )

    def test_empty_command_string(self, python_exe: str) -> None:
        """Test with empty command string."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN/THEN
            with pytest.raises(ValueError, match="command must be a non-empty string"):
                session.run_subprocess(command="", args=["-c", "print('test')"])

    def test_whitespace_only_command(self, python_exe: str) -> None:
        """Test with whitespace-only command string."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN/THEN
            with pytest.raises(ValueError, match="command must be a non-empty string"):
                session.run_subprocess(command="   ", args=["-c", "print('test')"])

    def test_nonexistent_command(self) -> None:
        """Test with non-existent command."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command="nonexistent_command_12345",
                args=["arg1"],
            )

            # Wait for failure
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status is not None
            assert session.action_status.state == ActionState.FAILED

    def test_callback_integration(self, python_exe: str) -> None:
        """Test callback invocation during subprocess execution."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('test')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert callback.call_count >= 1
            # Check that callback was called with session_id and ActionStatus
            callback.assert_called_with(
                session_id, ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            )

    @pytest.mark.skipif(not is_posix(), reason="Posix-only test.")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    def test_user_context_posix(
        self, posix_target_user: PosixSessionUser, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test subprocess runs as configured user on POSIX."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            user=posix_target_user,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import os; print(os.getuid())"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

    @pytest.mark.skipif(not is_windows(), reason="Windows-only test.")
    @pytest.mark.xfail(not has_windows_user(), reason=WIN_SET_TEST_ENV_VARS_MESSAGE)
    @pytest.mark.timeout(90)
    def test_user_context_windows(self, windows_user: WindowsSessionUser, python_exe: str) -> None:
        """Test subprocess runs as configured user on Windows."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            user=windows_user,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import os; print(os.getlogin())"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

    def test_multiple_sequential_calls(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test multiple run_subprocess calls in sequence."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN - First subprocess
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('first')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert session.state == SessionState.READY
            assert "first" in caplog.messages

            # WHEN - Second subprocess
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('second')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert "second" in caplog.messages
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

    def test_subprocess_with_entered_environment(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test run_subprocess within environment context with use_session_env_vars=True."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        environment = Environment_2023_09(
            name="TestEnv",
            variables={
                "TEST_ENV_VAR": EnvironmentVariableValueString_2023_09("test_value"),
            },
        )

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # Enter environment
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "import os; print(os.environ.get('TEST_ENV_VAR', 'MISSING'))",
                ],
                use_session_env_vars=True,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert "test_value" in caplog.messages

    def test_log_banner_message_with_custom_message(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that log_banner_message logs a custom banner when provided."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        custom_message = "Running Custom Subprocess"

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('test')"],
                log_banner_message=custom_message,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Check that the banner was logged with the custom message
            assert "==============================================" in caplog.text
            assert f"--------- {custom_message}" in caplog.text

    def test_log_banner_message_none_no_banner(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that no banner is logged when log_banner_message is None."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            caplog.clear()

            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('test')"],
                log_banner_message=None,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Check that no custom banner was logged when log_banner_message is None
            # There might be other banners from session setup, but we verify the custom message isn't there
            assert (
                "--------- Running" not in caplog.text
                or "--------- Running Custom Subprocess" not in caplog.text
            )

    def test_log_banner_message_empty_string_no_banner(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that no banner is logged when log_banner_message is an empty string."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            caplog.clear()

            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('test')"],
                log_banner_message="",
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Empty string is falsy, so no banner should be logged
            # Verify no empty message banner - check that all banner messages are from expected sources
            assert "--------- " not in caplog.text or caplog.text.count(
                "--------- "
            ) == caplog.text.count("--------- Running") + caplog.text.count(
                "--------- Entering"
            ) + caplog.text.count(
                "--------- Exiting"
            )

    def test_log_banner_message_with_special_characters(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that log_banner_message handles special characters correctly."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        custom_message = "Running: Test $VAR & Special <Chars>"

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('test')"],
                log_banner_message=custom_message,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Check that the banner was logged with the special characters intact
            assert f"--------- {custom_message}" in caplog.text

    def test_log_banner_message_multiple_calls_different_messages(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test multiple run_subprocess calls with different banner messages."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN - First subprocess with banner
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('first')"],
                log_banner_message="First Subprocess",
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert "--------- First Subprocess" in caplog.text

            # WHEN - Second subprocess with different banner
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('second')"],
                log_banner_message="Second Subprocess",
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            assert "--------- First Subprocess" in caplog.text
            assert "--------- Second Subprocess" in caplog.text

    def test_openjd_progress_message_triggers_callback(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that openjd_progress messages trigger callback with progress value."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('openjd_progress: 50.0')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Verify callback was called with progress
            progress_calls = [
                call
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].progress == 50.0
            ]
            assert len(progress_calls) > 0, "Callback should be invoked with progress=50.0"

    def test_openjd_status_message_triggers_callback(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that openjd_status messages trigger callback with status message."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('openjd_status: Processing data')"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Verify callback was called with status message
            status_calls = [
                call
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].status_message == "Processing data"
            ]
            assert (
                len(status_calls) > 0
            ), "Callback should be invoked with status_message='Processing data'"

    def test_multiple_openjd_progress_messages(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test multiple openjd_progress messages trigger callbacks with correct values."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "print('openjd_progress: 25.0'); print('openjd_progress: 50.0'); print('openjd_progress: 75.0')",
                ],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Verify callbacks were called with each progress value
            progress_values = [
                call[0][1].progress
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].progress is not None
            ]
            assert 25.0 in progress_values, "Callback should be invoked with progress=25.0"
            assert 50.0 in progress_values, "Callback should be invoked with progress=50.0"
            assert 75.0 in progress_values, "Callback should be invoked with progress=75.0"

    def test_multiple_openjd_status_messages(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test multiple openjd_status messages trigger callbacks with correct values."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "print('openjd_status: Starting'); print('openjd_status: Processing'); print('openjd_status: Finishing')",
                ],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY
            # Verify callbacks were called with each status message
            status_messages = [
                call[0][1].status_message
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].status_message is not None
            ]
            assert (
                "Starting" in status_messages
            ), "Callback should be invoked with status_message='Starting'"
            assert (
                "Processing" in status_messages
            ), "Callback should be invoked with status_message='Processing'"
            assert (
                "Finishing" in status_messages
            ), "Callback should be invoked with status_message='Finishing'"

    def test_openjd_progress_and_status_combined(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that both openjd_progress and openjd_status messages work together."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "print('openjd_status: Starting task'); print('openjd_progress: 33.3'); print('openjd_status: Halfway done'); print('openjd_progress: 66.6'); print('openjd_status: Almost complete'); print('openjd_progress: 100.0')",
                ],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY

            # Verify progress callbacks
            progress_values = [
                call[0][1].progress
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].progress is not None
            ]
            assert 33.3 in progress_values
            assert 66.6 in progress_values
            assert 100.0 in progress_values

            # Verify status callbacks
            status_messages = [
                call[0][1].status_message
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].status_message is not None
            ]
            assert "Starting task" in status_messages
            assert "Halfway done" in status_messages
            assert "Almost complete" in status_messages

    def test_openjd_fail_message_triggers_callback(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that openjd_fail messages trigger callback with fail message."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN
            session.run_subprocess(
                command=python_exe,
                args=["-c", "print('openjd_fail: Something went wrong'); import sys; sys.exit(1)"],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            # Verify callback was called with fail message
            fail_calls = [
                call
                for call in callback.call_args_list
                if len(call[0]) == 2 and call[0][1].fail_message == "Something went wrong"
            ]
            assert (
                len(fail_calls) > 0
            ), "Callback should be invoked with fail_message='Something went wrong'"

    def test_openjd_progress_invalid_value_ignored(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that invalid openjd_progress values are ignored and don't crash."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()

        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            callback=callback,
        ) as session:
            # WHEN - Use invalid progress values
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    "print('openjd_progress: not_a_number'); print('openjd_progress: -10'); print('openjd_progress: 150'); print('test completed')",
                ],
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - Should complete successfully despite invalid progress values
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert "test completed" in caplog.text

    def test_cancelation_of_running_subprocess(self, python_exe: str) -> None:
        """Test cancelation of a running subprocess."""
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN - Start a long-running subprocess
            session.run_subprocess(
                command=python_exe,
                args=["-c", "import time; print('Starting'); time.sleep(10); print('End')"],
            )

            # Give it a moment to start
            time.sleep(0.5)

            # Cancel the action while it's running
            session.cancel_action()

            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - Verify terminate-based cancelation behavior
            assert session.state == SessionState.READY_ENDING
            assert session.action_status is not None
            assert session.action_status.state == ActionState.CANCELED

            if is_posix():
                # On POSIX systems, SIGKILL results in exit code -9
                assert session.action_status.exit_code == -9
            else:
                # On Windows, process termination results in exit code 15
                assert session.action_status.exit_code == 15


class TestRunSubprocessIntegration:
    """Integration tests for Session.run_subprocess method."""

    def test_run_subprocess_within_environment_context(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test run_subprocess within environment context with both use_session_env_vars values.

        This integration test verifies:
        - Environment variables are inherited when use_session_env_vars=True
        - Environment variables are NOT inherited when use_session_env_vars=False
        - Proper environment lifecycle (enter, run subprocesses, exit)
        """
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        # Create an environment with test variables
        environment = Environment_2023_09(
            name="TestEnv",
            variables={
                "ENV_VAR_1": EnvironmentVariableValueString_2023_09("env_value_1"),
                "ENV_VAR_2": EnvironmentVariableValueString_2023_09("env_value_2"),
            },
        )

        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN - Enter the environment
            identifier = session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert session.state == SessionState.READY

            # WHEN - Run subprocess with use_session_env_vars=True
            caplog.clear()
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    'import os; v1 = os.environ.get("ENV_VAR_1", "MISSING"); v2 = os.environ.get("ENV_VAR_2", "MISSING"); print(f"ENV_VAR_1={v1} ENV_VAR_2={v2}")',
                ],
                use_session_env_vars=True,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - Verify environment variables are inherited
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert "ENV_VAR_1=env_value_1 ENV_VAR_2=env_value_2" in caplog.text

            # WHEN - Run subprocess with use_session_env_vars=False
            caplog.clear()
            session.run_subprocess(
                command=python_exe,
                args=[
                    "-c",
                    'import os; v1 = os.environ.get("ENV_VAR_1", "MISSING"); v2 = os.environ.get("ENV_VAR_2", "MISSING"); print(f"ENV_VAR_1={v1} ENV_VAR_2={v2}")',
                ],
                use_session_env_vars=False,
            )

            # Wait for completion
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - Verify environment variables are NOT inherited
            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert "ENV_VAR_1=MISSING ENV_VAR_2=MISSING" in caplog.text

            # WHEN - Exit the environment
            session.exit_environment(identifier=identifier)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN - Session should be in READY_ENDING state after exiting environment
            assert session.state == SessionState.READY_ENDING
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
