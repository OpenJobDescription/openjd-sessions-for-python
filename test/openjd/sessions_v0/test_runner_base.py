# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Optional, cast
from unittest.mock import MagicMock, call, patch

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import DataString as DataString_2023_09
from openjd.model.v2023_09 import (
    EmbeddedFileText as EmbeddedFileText_2023_09,
)
from openjd.model.v2023_09 import (
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)
from openjd.model.v2023_09 import (
    CommandString as CommandString_2023_09,
    ArgString as ArgString_2023_09,
)
from openjd.sessions import ActionState, PosixSessionUser, WindowsSessionUser
from openjd.sessions._embedded_files import EmbeddedFilesScope
from openjd.sessions._os_checker import is_posix, is_windows

from openjd.sessions._runner_base import (
    MAX_INT_FIELD_VALUE,
    NotifyCancelMethod,
    ScriptRunnerBase,
    ScriptRunnerState,
    TerminateCancelMethod,
)
from openjd.sessions._tempdir import TempDir

from .conftest import (
    build_logger,
    collect_queue_messages,
    has_posix_target_user,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)


# For testing, since ScriptRunnerBase is an abstract base class.
class TerminatingRunner(ScriptRunnerBase):
    _cancel_called = False

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        self._cancel_called = True
        self._cancel(TerminateCancelMethod())


class NotifyingRunner(ScriptRunnerBase):
    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        self._cancel_called_at = datetime.now(timezone.utc)
        if time_limit is None:
            self._cancel(NotifyCancelMethod(timedelta(seconds=2)))
        else:
            self._cancel(NotifyCancelMethod(time_limit))


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path")
class TestScriptRunnerBase:
    test_env_vars: dict[str, Optional[str]] = {
        "FOO": "BAR",
        "dollar_sign": "This costs $100",
        "single_quote": "They're smart",
        "double_quote": 'They said, "Hello!"',
        "back_slash": "C:\\Windows\\System32",
        "caret_symbol": "Up^Down",
        "pipe_symbol": "Left|Right",
        "ampersand_symbol": "Fish&Chips",
        "less_than": "1 < 2",
        "greater_than": "3 > 2",
        "asterisk_star": "Twinkle*twinkle",
        "question_mark": "Who? What? Where?",
        "colon_symbol": "Time: 12:00 PM",
        "semicolon_symbol": "Item1; Item2; Item3",
        "equal_sign": "1 + 1 = 2",
        "at_symbol": "user@example.com",
        "hash_symbol": "#1 Winner",
        "tilde_symbol": "Approximately~100",
        "percent_symbol": "50% off",
        "exclamation_mark": "Surprise!",
        "square_brackets": "Array[5]",
        "win_injection1": "& Get-Process",
        "win_injection2": "; Get-Process",
        "win_injection3": "| Get-Process",
        "win_injection4": "& Get-Process",
        "win_injection5": "nGet-ChildItem C:\\",
        "win_injection6": "rnStart-Process notepad.exe",
        "win_injection7": "$(Get-Process)",
        "posix_injection1": "$(whoami)",
        "posix_injection2": "; whoami",
        "posix_injection3": "| whoami",
    }

    def test_initialized(self, tmp_path: Path) -> None:
        # Test the property getters for a runner that is only initialized.

        # GIVEN
        with TerminatingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            pass

        # THEN
        assert runner.state == ScriptRunnerState.READY
        assert runner.exit_code is None

    def test_basic_run(self, tmp_path: Path, python_exe: str) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        callback = MagicMock()
        with TerminatingRunner(
            logger=MagicMock(), session_working_directory=tmp_path, callback=callback
        ) as runner:
            # WHEN
            runner._run([python_exe, "-c", "import time; time.sleep(0.25)"])

            # THEN
            assert runner.state == ScriptRunnerState.RUNNING
            assert runner.exit_code is None
            current_wait_seconds = 0
            while runner.state == ScriptRunnerState.RUNNING and current_wait_seconds < 10:
                time.sleep(1)
                current_wait_seconds += 1
            assert runner.state == ScriptRunnerState.SUCCESS
            assert runner.exit_code == 0
        callback.assert_has_calls([call(ActionState.RUNNING), call(ActionState.SUCCESS)])

    @pytest.mark.parametrize("attempt", [i for i in range(0, 100)])
    def test_fast_run_no_deadlock(self, attempt: int, tmp_path: Path) -> None:
        # Run a really fast command multiple times. We're trying to ensure that there's no
        # deadlock in between the _run() and _on_process_exit() method obtaining the lock.
        # This is a probabilistic test; it is not 100% reliable for reproducing the deadlock.

        # GIVEN
        callback = MagicMock()
        with TerminatingRunner(
            logger=MagicMock(), session_working_directory=tmp_path, callback=callback
        ) as runner:
            # WHEN
            runner._run(["whoami"])

            # THEN
            # Nothing to check. We just want to run it fast. The test will deadlock if
            # we have a problem. Just wait for the application to exit
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.0001)

    def test_working_dir_is_cwd(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test to make sure that the current working dir of the command that's run is
        # the startup directory.

        # GIVEN
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmp_path, startup_directory=tmp_path
        ) as runner:
            # WHEN
            runner._run([python_exe, "-c", "import os; print(os.getcwd())"])
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        messages = collect_queue_messages(message_queue)
        assert str(tmp_path) in messages

    def test_failing_run(self, tmp_path: Path, python_exe: str) -> None:
        # Test to make sure that we properly communicate a process with
        # non-zero return as

        # GIVEN
        with TerminatingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            # WHEN
            runner._run([python_exe, "-c", "import sys; sys.exit(1)"])

            # THEN
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)
            assert runner.state == ScriptRunnerState.FAILED
            assert runner.exit_code == 1

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_fail_to_run(
        self, tmp_path: Path, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Test that we don't blow up in an unexpected way when we cannot actually
        # run the subprocess for some reason.

        # GIVEN
        logger = build_logger(queue_handler)
        runner = TerminatingRunner(logger=logger, session_working_directory=tmp_path)

        # WHEN
        if is_posix():
            runner._run([str(tmp_path)])
        else:
            runner._run(["test_failed_command"])

        # This process should finish within 25s
        for _ in range(125):
            if runner.state in (
                ScriptRunnerState.FAILED,
                ScriptRunnerState.SUCCESS,
                ScriptRunnerState.TIMEOUT,
            ):
                break
            # Give the command time to fail out.
            time.sleep(0.2)

        messages = collect_queue_messages(message_queue)

        # THEN
        if is_windows():
            # Note: On posix, we embed the command in a shell script. That shell script
            # starts running just fine, but then will return non-0.
            assert any(
                item.startswith("Process failed to start") for item in messages
            ), "Logged error message is not correct."
        assert runner.state == ScriptRunnerState.FAILED
        assert runner.exit_code != 0

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_run_with_env_vars(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        logger = build_logger(queue_handler)

        with TerminatingRunner(
            logger=logger, session_working_directory=tmp_path, os_env_vars=self.test_env_vars
        ) as runner:
            # WHEN
            runner._run(
                [
                    python_exe,
                    "-c",
                    r"import os;print(*(f'{k} = {v}' for k,v in os.environ.items()), sep='\n')",
                ]
            )

            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        messages = collect_queue_messages(message_queue)
        for key, value in self.test_env_vars.items():
            if is_windows():
                assert f"{key.upper()} = {value}" in messages
            else:
                assert f"{key} = {value}" in messages

    @pytest.mark.skipif(not is_posix(), reason="posix-only test")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    @pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
    def test_run_as_posix_user(
        self,
        posix_target_user: PosixSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that we run the process as a specific desired user

        # GIVEN
        tmpdir = TempDir(user=posix_target_user)
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmpdir.path, user=posix_target_user
        ) as runner:
            # WHEN
            runner._run(
                [
                    # Note: Intentionally not `sys.executable`. Reasons:
                    #  1) This is a cross-account command, and python_exe may be in a user-specific venv
                    #  2) This test is, generally, intended to be run in a docker container where the system
                    #     python is the correct version that we want to run under.
                    "python",
                    "-c",
                    "import os; print(os.getuid())",
                ]
            )
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        assert runner.exit_code == 0
        messages = collect_queue_messages(message_queue)
        assert str(os.getuid()) not in messages  # type: ignore
        import pwd

        uid = pwd.getpwnam(posix_target_user.user).pw_uid  # type: ignore
        assert str(uid) in messages

        tmpdir.cleanup()

    @pytest.mark.skipif(not is_posix(), reason="posix-only test")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    @pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
    def test_run_as_posix_user_with_env_vars(
        self,
        posix_target_user: PosixSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that we run the process as a specific desired user with env vars defined as expected

        # GIVEN
        tmpdir = TempDir(user=posix_target_user)
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger,
            session_working_directory=tmpdir.path,
            user=posix_target_user,
            os_env_vars=self.test_env_vars,
        ) as runner:
            # WHEN
            runner._run(
                [
                    # Note: Intentionally not `python_exe`. Reasons:
                    #  1) This is a cross-account command, and python_exe may be in a user-specific venv
                    #  2) This test is, generally, intended to be run in a docker container where the system
                    #     python is the correct version that we want to run under.
                    "python",
                    "-c",
                    r"import os;print(*(f'{k} = {v}' for k,v in os.environ.items()), sep='\n')",
                ]
            )
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        messages = collect_queue_messages(message_queue)
        for key, value in self.test_env_vars.items():
            assert f"{key} = {value}" in messages

        tmpdir.cleanup()

    @pytest.mark.skipif(not is_windows(), reason="Windows-only test")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    @pytest.mark.timeout(90)
    def test_run_as_windows_user(
        self,
        windows_user: WindowsSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that we run the process as a specific desired user

        # GIVEN
        from openjd.sessions._win32._helpers import get_process_user  # type: ignore

        tmpdir = TempDir(user=windows_user)
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmpdir.path, user=windows_user
        ) as runner:
            # WHEN
            runner._run(["whoami"])
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        assert runner.exit_code == 0
        messages = collect_queue_messages(message_queue)
        process_user = get_process_user()
        assert all([process_user not in message for message in messages])
        assert any(windows_user.user in message for message in messages)

        tmpdir.cleanup()

    @pytest.mark.skipif(not is_windows(), reason="Windows-only test")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    @pytest.mark.timeout(90)
    def test_failed_run_as_windows_user(
        self,
        windows_user: WindowsSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test we fail properly when given a command that does not exist

        # GIVEN
        tmpdir = TempDir(user=windows_user)
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmpdir.path, user=windows_user
        ) as runner:
            # WHEN
            runner._run(["test_not_a_command"])
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        assert runner.state == ScriptRunnerState.FAILED
        assert runner.exit_code is None
        messages = collect_queue_messages(message_queue)
        assert messages == ["openjd_fail: Could not find executable file: test_not_a_command"]

        tmpdir.cleanup()

    @pytest.mark.skipif(not is_windows(), reason="Windows-only test")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    @pytest.mark.timeout(30)
    @pytest.mark.usefixtures("message_queue", "queue_handler", "windows_user")
    def test_run_as_windows_user_with_env_vars(
        self,
        windows_user: WindowsSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that we run the process as a specific desired user with env vars defined as expected

        # GIVEN
        tmpdir = TempDir(user=windows_user)
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger,
            session_working_directory=tmpdir.path,
            user=windows_user,
            os_env_vars=self.test_env_vars,
        ) as runner:
            # WHEN
            runner._run(
                [
                    # Note: Intentionally not `python_exe`. Reasons:
                    #  1) This is a cross-account command, and python_exe may be in a user-specific venv
                    #  2) This test is, generally, intended to be run in a docker container where the system
                    #     python is the correct version that we want to run under.
                    "python",
                    "-c",
                    r"import os;print(*(f'{k} = {v}' for k,v in os.environ.items()), sep='\n')",
                ]
            )
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)

        # THEN
        messages = collect_queue_messages(message_queue)
        for key, value in self.test_env_vars.items():
            assert f"{key.upper()} = {value}" in messages

        tmpdir.cleanup()

    @pytest.mark.skipif(not is_posix(), reason="posix-specific test")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    @pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
    @pytest.mark.timeout(40)
    def test_does_not_inherit_env_vars_posix(
        self,
        posix_target_user: PosixSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Security test.
        # Run a command that tries to read from this process's environment. It should not be able
        # to obtain values from it.
        # Only the cross-user case ensures that environment is not passed through; this is to ensure
        # that sensitive information that is defines in the initiating process' environment is not
        # propagated through a user boundary to the subprocess.

        # GIVEN
        tmpdir = TempDir(user=posix_target_user)
        var_name = "TEST_DOES_NOT_INHERIT_ENV_VARS_VAR"
        os.environ[var_name] = "TEST_VALUE"
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmpdir.path, user=posix_target_user
        ) as runner:
            # WHEN
            runner._run(
                [
                    # Note: Intentionally not `python_exe`. Reasons:
                    #  1) This is a cross-account command, and python_exe may be in a user-specific venv
                    #  2) This test is, generally, intended to be run in a docker container where the system
                    #     python is the correct version that we want to run under.
                    "python",
                    "-c",
                    f"import time; import os; time.sleep(0.25); print(os.environ.get('{var_name}', 'NOT_PRESENT')); print(os.environ)",
                ]
            )

            # THEN
            assert runner.state == ScriptRunnerState.RUNNING
            assert runner.exit_code is None
            current_wait_seconds = 0
            while runner.state == ScriptRunnerState.RUNNING and current_wait_seconds < 10:
                time.sleep(1)
                current_wait_seconds += 1
            assert runner.state == ScriptRunnerState.SUCCESS
            assert runner.exit_code == 0

        messages = collect_queue_messages(message_queue)
        assert os.environ[var_name] not in messages
        assert "NOT_PRESENT" in messages

    @pytest.mark.skipif(not is_windows(), reason="Windows-specific test")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    def test_does_not_inherit_env_vars_windows(
        self,
        windows_user: WindowsSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Security test.
        # Run a command that tries to read from this process's environment. It should not be able
        # to obtain values from it.
        # Only the cross-user case ensures that environment is not passed through; this is to ensure
        # that sensitive information that is defines in the initiating process' environment is not
        # propagated through a user boundary to the subprocess.

        # GIVEN
        tmpdir = TempDir(user=windows_user)
        var_name = "TEST_DOES_NOT_INHERIT_ENV_VARS_VAR"
        os.environ[var_name] = "TEST_VALUE"
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmpdir.path, user=windows_user
        ) as runner:
            # WHEN
            py_script = f"import os; v=os.environ.get('{var_name}'); print('NOT_PRESENT' if v is None else v)"
            # Use the default 'python' rather than 'sys.executable' since we typically do not have access to
            # python_exe when running with impersonation since it's in a hatch environment for the local user.
            runner._run(["python", "-c", py_script])

            # THEN
            assert runner.state == ScriptRunnerState.RUNNING
            assert runner.exit_code is None
            current_wait_seconds = 0
            while runner.state == ScriptRunnerState.RUNNING and current_wait_seconds < 10:
                time.sleep(1)
                current_wait_seconds += 1
            assert runner.state == ScriptRunnerState.SUCCESS
            assert runner.exit_code == 0

        messages = collect_queue_messages(message_queue)
        assert os.environ[var_name] not in messages
        assert "NOT_PRESENT" in messages

    def test_cannot_run_twice(self, tmp_path: Path, python_exe: str) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        callback = MagicMock()
        with TerminatingRunner(
            logger=MagicMock(), session_working_directory=tmp_path, callback=callback
        ) as runner:
            # WHEN
            runner._run([python_exe, "-c", "print('hello')"])

            # THEN
            with pytest.raises(RuntimeError):
                runner._run([python_exe, "-c", "print('hello')"])

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_run_action(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Run a test of the _run_action method that makes sure that the action runs
        # and the format strings are evaluated.

        # GIVEN
        action = Action_2023_09(
            command=CommandString_2023_09("{{Task.PythonInterpreter}}"),
            args=[ArgString_2023_09("{{Task.ScriptFile}}")],
            timeout=(5),
        )
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        symtab = SymbolTable(
            source={
                "Task.PythonInterpreter": python_exe,
                "Task.ScriptFile": str(python_app_loc),
            }
        )
        logger = build_logger(queue_handler)
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            # WHEN
            runner._run_action(action, symtab)
            # wait for the process to exit
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.TIMEOUT
        messages = collect_queue_messages(message_queue)
        # The application prints out 0, ..., 9 once a second for 10s.
        # If it ended early, then we printed the first but not the last.
        print(messages)
        assert "Log from test 0" in messages
        assert "Log from test 9" not in messages

    @pytest.mark.parametrize(
        argnames=("default_timeout_seconds", "action_timeout_seconds", "expected_seconds"),
        argvalues=(
            pytest.param(1, 5, 5, id="action-timeout-prevails"),
            pytest.param(5, 1, 1, id="action-timeout-prevails-when-smaller"),
            pytest.param(2, None, 2, id="default-applied"),
            pytest.param(None, 7, 7, id="action-only"),
            pytest.param(None, None, None, id="no-timeout"),
        ),
    )
    def test_run_action_effective_timeout(
        self,
        tmp_path: Path,
        default_timeout_seconds: Optional[int],
        action_timeout_seconds: Optional[int],
        expected_seconds: Optional[int],
        python_exe: str,
    ) -> None:
        """The effective time limit is the action's timeout if it has one, else the
        caller's default, else none at all.

        Asserts the time limit `_run_action` hands to `_run`, with no subprocess
        and no wall clock involved.

        This replaces an end-to-end version that started a child printing one line
        per second and asserted a +/-1 second window on its output
        (`"Log from test {T-1}" in messages`). That coupled three unrelated
        things -- timeout *selection*, `threading.Timer` scheduling, and child
        process startup latency -- and only the first is what this test is named
        for. The Timer is armed before the child is submitted to the pool, so the
        Timer's clock starts before the interpreter even launches; any host slow
        enough to delay startup past a second failed the assertion while the
        product behaved correctly. Measured: injecting a 1.5s startup delay
        against a 2s timeout fails the old assertion with the runner correctly in
        TIMEOUT. It was the suite's most persistent false red.
        """
        # GIVEN
        default_timeout = (
            timedelta(seconds=default_timeout_seconds)
            if default_timeout_seconds is not None
            else None
        )
        action = Action_2023_09(
            command=CommandString_2023_09("{{Task.PythonInterpreter}}"),
            args=[ArgString_2023_09("-c"), ArgString_2023_09("pass")],
            timeout=action_timeout_seconds,
        )
        symtab = SymbolTable(source={"Task.PythonInterpreter": python_exe})
        captured: list[Optional[timedelta]] = []

        with TerminatingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            # WHEN
            with patch.object(
                runner,
                "_run",
                side_effect=lambda args, time_limit=None: captured.append(time_limit),
            ):
                runner._run_action(action, symtab, default_timeout=default_timeout)

        # THEN
        assert len(captured) == 1, "the action was not launched exactly once"
        expected = timedelta(seconds=expected_seconds) if expected_seconds is not None else None
        assert captured[0] == expected

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    @pytest.mark.parametrize(
        argnames=("action_timeout_seconds", "expected_state"),
        argvalues=(
            pytest.param(2, ScriptRunnerState.TIMEOUT, id="timeout-terminates-the-action"),
            pytest.param(None, ScriptRunnerState.SUCCESS, id="no-timeout-runs-to-completion"),
        ),
    )
    def test_run_action_timeout_is_enforced(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        action_timeout_seconds: Optional[int],
        expected_state: ScriptRunnerState,
        python_exe: str,
    ) -> None:
        """A declared timeout really does terminate the action, and no timeout
        really does let it finish.

        The end-to-end half of the coverage above, kept deliberately loose: it
        asserts only the terminal state and that the child did or did not reach its
        last line. It says nothing about *when* the timeout landed, because the
        exact second the child reaches depends on how promptly the host scheduled
        it -- which is what made the previous version of this test flaky.
        """
        # GIVEN: a child that prints one line a second for 20 seconds
        action = Action_2023_09(
            command=CommandString_2023_09("{{Task.PythonInterpreter}}"),
            args=[ArgString_2023_09("{{Task.ScriptFile}}")],
            timeout=action_timeout_seconds,
        )
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        symtab = SymbolTable(
            source={
                "Task.PythonInterpreter": python_exe,
                "Task.ScriptFile": str(python_app_loc),
            }
        )
        logger = build_logger(queue_handler)
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            # WHEN
            runner._run_action(action, symtab)
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.2)

        # THEN
        assert runner.state == expected_state
        messages = collect_queue_messages(message_queue)
        if expected_state is ScriptRunnerState.TIMEOUT:
            # It was cut short. Which line it got to is a scheduling detail.
            assert "Log from test 19" not in messages
        else:
            assert "Log from test 19" in messages

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    @pytest.mark.parametrize(
        argnames="timeout_seconds",
        argvalues=(
            # Larger than datetime.timedelta can represent.
            pytest.param(86_400_000_000_000, id="over-timedelta-max"),
            # Representable by timedelta, but threading.Timer's deadline
            # arithmetic overflows CPython's 64-bit nanosecond time
            # representation.
            pytest.param(86_399_999_913_600, id="over-schedulable-but-in-timedelta"),
            pytest.param(9_223_372_036_854_775_807, id="i64-max"),
        ),
    )
    def test_run_action_unenforceable_timeout_runs_unbounded(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        timeout_seconds: int,
    ) -> None:
        """A timeout too large to schedule must not raise; the action runs.

        The 2023-09 <Action> schema puts no upper bound on `timeout`, so these
        values are template-legal. openjd-rs resolves them with
        `Duration::from_secs` and runs the action, so we do the same rather than
        raising OverflowError out of the public Session API (which used to leave
        the Session stuck in RUNNING with no terminal ActionStatus).
        """
        # GIVEN
        action = Action_2023_09(
            command=CommandString_2023_09("{{Task.Command}}"),
            args=[ArgString_2023_09("ok")],
            timeout=timeout_seconds,
        )
        symtab = SymbolTable(source={"Task.Command": "echo" if is_posix() else "cmd.exe"})
        logger = build_logger(queue_handler)

        # WHEN
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            runner._run_action(action, symtab)
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.2)

            # THEN: it ran, with no time limit scheduled
            assert runner.state == ScriptRunnerState.SUCCESS
            assert runner._runtime_limit is None
        messages = collect_queue_messages(message_queue)
        assert any("larger than this runtime can enforce" in m for m in messages)

    @pytest.mark.usefixtures("queue_handler")
    def test_run_action_over_range_timeout_fails_action(
        self,
        tmp_path: Path,
        queue_handler: QueueHandler,
    ) -> None:
        """An over-range timeout fails the action, matching openjd-rs.

        openjd-rs rejects a literal above i64::MAX at parse time and its runtime
        parses a resolved one with str::parse, which fails rather than
        saturating -- so the action must fail through the normal failure path
        instead of running. See MAX_INT_FIELD_VALUE.
        """
        # GIVEN
        action = Action_2023_09(
            command=CommandString_2023_09("echo"),
            args=[ArgString_2023_09("ok")],
            timeout=MAX_INT_FIELD_VALUE + 1,
        )
        logger = build_logger(queue_handler)

        # WHEN
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            runner._run_action(action, SymbolTable())

            # THEN
            assert runner.state == ScriptRunnerState.FAILED
            assert runner._runtime_limit is None

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_run_action_bad_formatstring(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Run a test of the _run_action method when the input has a bad format string.
        # We shouldn't even try to run the action in this case, and fail out early.

        # GIVEN
        action = Action_2023_09(
            command=CommandString_2023_09("{{Task.PythonInterpreter}}"),
            args=[ArgString_2023_09("{{Task.ScriptFile}}")],
            timeout=1,
        )
        symtab = SymbolTable()
        logger = build_logger(queue_handler)
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            # WHEN
            runner._run_action(action, symtab)

        # THEN
        assert runner.state == ScriptRunnerState.FAILED
        assert runner.exit_code is None
        messages = collect_queue_messages(message_queue)
        assert any(m.startswith("openjd_fail") for m in messages)

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_cancel_terminate(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that the subprocess is terminated when doing a TERMINATE style
        # cancelation

        # GIVEN
        callback = MagicMock()
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmp_path, callback=callback
        ) as runner:
            python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
            runner._run([python_exe, str(python_app_loc)])

            # WHEN
            runner.cancel()

            # THEN
            # Wait for the app to exit
            while runner.state == ScriptRunnerState.CANCELING:
                time.sleep(0.2)
            assert runner.state == ScriptRunnerState.CANCELED
            assert runner.exit_code != 0
            time.sleep(1)  # Some time for the cancel callback to be invoked.
            callback.assert_has_calls([call(ActionState.RUNNING), call(ActionState.CANCELED)])
        messages = collect_queue_messages(message_queue)
        # Didn't get to the end of the application run
        assert "Log from test 9" not in messages

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    @pytest.mark.xfail(not is_posix(), reason="Signals not yet implemented for non-posix")
    def test_run_with_time_limit(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that the subprocess is terminated when doing a TERMINATE style
        # cancelation

        # GIVEN
        logger = build_logger(queue_handler)
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()

            # WHEN
            runner._run([python_exe, str(python_app_loc)], time_limit=timedelta(seconds=1))

            # THEN
            # Wait until the process exits. We'll be in CANCELING state between when the timeout is reached
            # and the process finally exits.
            while runner.state in (ScriptRunnerState.RUNNING, ScriptRunnerState.CANCELING):
                time.sleep(0.1)
            assert runner.state == ScriptRunnerState.TIMEOUT
            assert runner.exit_code != 0
            assert cast(TerminatingRunner, runner)._cancel_called
        messages = collect_queue_messages(message_queue)
        # Didn't get to the end of the application run
        assert "Log from test 9" not in messages

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_cancel_notify(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that NOTIFY_THEN_CANCEL first signals a SIGTERM and then a SIGKILL

        # GIVEN
        proc_id: Optional[int] = None
        logger = build_logger(queue_handler)
        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (
                Path(__file__).parent / "support_files" / "app_20s_run_ignore_signal.py"
            ).resolve()
            runner._run([python_exe, str(python_app_loc)])

            # WHEN
            secs = 2 if not is_windows() else 5
            time.sleep(secs)  # Give the process a little time to do something
            now = datetime.now(timezone.utc)

            assert runner._process is not None
            assert runner._process._pid is not None
            proc_id = runner._process._pid

            runner.cancel(time_limit=timedelta(seconds=2))

            # THEN
            assert runner.state == ScriptRunnerState.CANCELING
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.CANCELING:
                time.sleep(0.1)
            # This should be CANCELED rather than TIMEOUT because this test is manually calling
            # the cancel() method rather than letting the action reach its runtime limit.
            assert (
                runner.state == ScriptRunnerState.CANCELED
            )  # TODO - This test is flaky. Sometimes, this is 'RUNNING'
            assert runner.exit_code != 0
        messages = collect_queue_messages(message_queue)
        assert "Trapped" in messages
        trapped_idx = messages.index("Trapped")
        process_exit_idx = messages.index(
            f"Process pid {proc_id} exited with code: {runner.exit_code} (unsigned) / {hex(runner.exit_code)} (hex)"
        )
        # Should be at least one more number printed after the Trapped
        # to indicate that we didn't immediately terminate the script.
        assert any(msg.isdigit() for msg in messages[trapped_idx + 1 : process_exit_idx])
        # Didn't get to the end
        assert "Log from test 9" not in messages
        # Notification file exists
        assert os.path.exists(tmp_path / "cancel_info.json")
        with open(tmp_path / "cancel_info.json", "r") as file:
            notification_data_json = file.read()
        notification_data = json.loads(notification_data_json)
        assert len(notification_data) == 1
        assert "NotifyEnd" in notification_data
        assert notification_data["NotifyEnd"][-1] == "Z"
        # Stripping the Z removes timezone information. Need to ensure it's not interpreted as local
        time_end = datetime.fromisoformat(notification_data["NotifyEnd"][:-1]).replace(
            tzinfo=timezone.utc
        )
        # Timestamp should be around 2s from cancel signal, but give a 1s window
        # for timing differences.
        delta_t = time_end - now
        assert timedelta(seconds=1) < delta_t < timedelta(seconds=3)

    @pytest.mark.skipif(not is_posix(), reason="posix-only test")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    @pytest.mark.requires_cap_kill
    def test_cancel_notify_direct_signal_with_cap_kill(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test for Linux hosts, that when CAP_KILL is in the permitted (and possibly effective)
        # capability set(s), that the runner will:
        #   1. directly signal the subprocess to notify
        #   2. retain the status of CAP_KILL in the thread's effective capability set

        # GIVEN
        logger = build_logger(queue_handler)

        from openjd.sessions._linux._capabilities import (
            _has_capability,
            _get_libcap,
            CAP_KILL,
            CapabilitySetType,
        )

        # Record whether CAP_KILL is in the effective capability set before
        # notifying the subprocess
        libcap = _get_libcap()
        assert libcap is not None, "Libcap not found"
        caps = libcap.cap_get_proc()
        cap_kill_was_effective = _has_capability(
            libcap=libcap,
            caps=caps,
            capability=CAP_KILL,
            capability_set_type=CapabilitySetType.EFFECTIVE,
        )

        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            # Path to compiled C program that outputs the PID of the process sending the signal
            output_signal_sender_app_loc = (
                Path(__file__).parent / "support_files" / "output_signal_sender"
            ).resolve()
            assert output_signal_sender_app_loc.exists(), "output_signal_sender is not compiled."
            runner._run([str(output_signal_sender_app_loc)])

            # WHEN
            secs = 2 if not is_windows() else 5
            time.sleep(secs)  # Give the process a little time to do something
            runner.cancel(time_limit=timedelta(seconds=2))

            # THEN
            for _ in range(10):
                if runner.state == ScriptRunnerState.CANCELED:
                    break
                time.sleep(1)
            else:
                # Terminate the subprocess
                runner.cancel()
                assert False, "output_signal_sender process did not exit when sent SIGTERM"
            assert runner.exit_code == 0

        # Collect stdout lines. Based on the code of output_signal_sender.c, only a single
        # line should be output with the PID of the process that sent the SIGINT signal.
        # Extracting the log line requires finding the preceeding log line emitted by the runner,
        # then taking the following line and parsing it as an integer
        messages = collect_queue_messages(message_queue)
        for i, message in enumerate(messages):
            if message.startswith('INTERRUPT: Sending signal "term" to process '):
                break
        else:
            assert False, "Could not find log line before stdout"
        pid_line = messages[i + 1]
        signal_sender_pid = int(pid_line)

        current_pid = os.getpid()
        assert (
            current_pid == signal_sender_pid
        ), "The runner's subprocess was not directly signalled"

        # Assert that the presence/absence of CAP_KILL in the effective capability set
        # is unchanged after calling Runner.cancel()
        caps = libcap.cap_get_proc()
        cap_kill_effective_after_cancel = _has_capability(
            libcap=libcap,
            caps=caps,
            capability=CAP_KILL,
            capability_set_type=CapabilitySetType.EFFECTIVE,
        )
        assert (
            cap_kill_was_effective == cap_kill_effective_after_cancel
        ), "CAP_KILL added/removed from effetive set and persisted after cancelation"

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_cancel_double_cancel_notify(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that NOTIFY_THEN_CANCEL can be called twice, and the second time will
        # shrink the grace period

        # GIVEN
        logger = build_logger(queue_handler)
        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (
                Path(__file__).parent / "support_files" / "app_20s_run_ignore_signal.py"
            ).resolve()
            runner._run([python_exe, str(python_app_loc)])

            # WHEN
            secs = 2 if not is_windows() else 5
            time.sleep(secs)  # Give the process a little time to do something
            runner.cancel(time_limit=timedelta(seconds=15))
            runner.cancel(time_limit=timedelta(seconds=1 if not is_windows() else 3))

            # THEN
            assert runner.state == ScriptRunnerState.CANCELING
            # Wait until the process exits.
            while runner.state == ScriptRunnerState.RUNNING:
                time.sleep(0.1)
        # This should be CANCELED rather than TIMEOUT because this test is manually calling
        # the cancel() method rather than letting the action reach its runtime limit.
        assert runner.state == ScriptRunnerState.CANCELED
        assert runner.exit_code != 0
        messages = collect_queue_messages(message_queue)
        assert "Trapped" in messages
        # In this case, the total runtime of the app is 10s
        # so we know that if we didn't get the last index printed
        # then the second cancel took precidence.
        assert "Log from test 9" not in messages

    def test_materialize_files(self, tmp_path: Path) -> None:
        # A test that _materialize_files writes the given files to disk, and
        # populates its given symbol table.

        # GIVEN
        with NotifyingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                filename="test_materialize_files.txt",
                data=DataString_2023_09("some data"),
            )
            symtab = SymbolTable()

            # WHEN
            runner._materialize_files(EmbeddedFilesScope.STEP, [test_file], tmp_path, symtab)

        # THEN
        assert runner.state == ScriptRunnerState.READY
        assert os.path.exists(tmp_path / "test_materialize_files.txt")
        assert len(symtab.symbols) == 1

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_materialize_files_fails(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # A test that _materialize_files handles errors properly when it cannot write the
        # files to disk (e.g. because of permissions).

        # GIVEN
        logger = build_logger(queue_handler)
        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            dest_dir = (
                tmp_path / "a" / "file" / "path" / "that" / "definitely" / "does" / "not" / "exist"
            )
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                filename="test_materialize_files.txt",
                data=DataString_2023_09("some data"),
            )
            symtab = SymbolTable()

            # WHEN
            runner._materialize_files(EmbeddedFilesScope.STEP, [test_file], dest_dir, symtab)

        # THEN
        assert runner.state == ScriptRunnerState.FAILED
        assert not os.path.exists(dest_dir / "test_materialize_files.txt")
        messages = collect_queue_messages(message_queue)
        assert any(m.startswith("openjd_fail") for m in messages)
