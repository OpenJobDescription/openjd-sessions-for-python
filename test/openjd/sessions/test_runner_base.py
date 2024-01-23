# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Optional, cast
from unittest.mock import MagicMock, call

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
from openjd.sessions import ActionState, PosixSessionUser
from openjd.sessions._embedded_files import EmbeddedFilesScope
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._runner_base import (
    NotifyCancelMethod,
    ScriptRunnerBase,
    ScriptRunnerState,
    TerminateCancelMethod,
)
from openjd.sessions._tempdir import TempDir

from .conftest import build_logger, collect_queue_messages, has_posix_target_user


# For testing, since ScriptRunnerBase is an abstract base class.
class TerminatingRunner(ScriptRunnerBase):
    _cancel_called = False

    def cancel(self, *, time_limit: Optional[timedelta] = None) -> None:
        self._cancel_called = True
        self._cancel(TerminateCancelMethod())


class NotifyingRunner(ScriptRunnerBase):
    def cancel(self, *, time_limit: Optional[timedelta] = None) -> None:
        self._cancel_called_at = datetime.utcnow()
        if time_limit is None:
            self._cancel(NotifyCancelMethod(timedelta(seconds=2)))
        else:
            self._cancel(NotifyCancelMethod(time_limit))


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path")
class TestScriptRunnerBase:
    def test_initialized(self, tmp_path: Path) -> None:
        # Test the property getters for a runner that is only initialized.

        # GIVEN
        with TerminatingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            pass

        # THEN
        assert runner.state == ScriptRunnerState.READY
        assert runner.exit_code is None

    def test_basic_run(self, tmp_path: Path) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        callback = MagicMock()
        with TerminatingRunner(
            logger=MagicMock(), session_working_directory=tmp_path, callback=callback
        ) as runner:
            # WHEN
            runner._run([sys.executable, "-c", "import time; time.sleep(0.25)"])

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
            runner._run(["echo", ""])

            # THEN
            # Nothing to check. We just want to run it fast. The test will deadlock if
            # we have a problem. Just wait for the application to exit
            while runner.exit_code is None:
                time.sleep(0.0001)

    def test_working_dir_is_cwd(
        self, tmp_path: Path, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Test to make sure that the current working dir of the command that's run is
        # the startup directory.

        # GIVEN
        logger = build_logger(queue_handler)
        with TerminatingRunner(
            logger=logger, session_working_directory=tmp_path, startup_directory=tmp_path
        ) as runner:
            # WHEN
            runner._run([sys.executable, "-c", "import os; print(os.getcwd())"])
            # Wait until the process exits.
            while runner.exit_code is None:
                time.sleep(0.2)

        # THEN
        messages = collect_queue_messages(message_queue)
        assert str(tmp_path) in messages

    def test_failing_run(self, tmp_path: Path) -> None:
        # Test to make sure that we properly communicate a process with
        # non-zero return as

        # GIVEN
        with TerminatingRunner(logger=MagicMock(), session_working_directory=tmp_path) as runner:
            # WHEN
            runner._run([sys.executable, "-c", "import sys; sys.exit(1)"])

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
            if runner.exit_code is not None:
                break
            # Give the command time to fail out.
            time.sleep(0.2)

        messages = collect_queue_messages(message_queue)

        # THEN
        if is_windows():
            assert any(
                item.startswith(
                    "Command not found: The term 'test_failed_command'"
                    " is not recognized as a name of a cmdlet"
                )
                for item in messages
            ), "Error message in Windows is not correct."
        assert runner.state == ScriptRunnerState.FAILED
        assert runner.exit_code != 0

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_run_with_env_vars(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        logger = build_logger(queue_handler)
        os_env_vars: dict[str, Optional[str]] = {
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
            "injection1": "& Get-Process",
            "injection2": "; Get-Process",
            "injection3": "| Get-Process",
            "injection4": "& Get-Process",
            "injection5": "nGet-ChildItem C:\\",
            "injection6": "rnStart-Process notepad.exe",
            "injection7": "$(Get-Process)",
        }

        with TerminatingRunner(
            logger=logger, session_working_directory=tmp_path, os_env_vars=os_env_vars
        ) as runner:
            # WHEN
            # Generate the Python code string
            code_lines = ["import os"]
            for key, value in os_env_vars.items():
                code_lines.append(f"print('{key} =', os.environ.get('{key}'))")

            code_str = "; ".join(code_lines)

            runner._run([sys.executable, "-c", code_str])

            # Wait until the process exits.
            while runner.exit_code is None:
                time.sleep(0.2)

        # THEN
        messages = collect_queue_messages(message_queue)
        for key, value in os_env_vars.items():
            assert f"{key} = {value}" in messages

    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason="Must be running inside of the sudo_environment testing container.",
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
                    #  1) This is a cross-account command, and sys.executable may be in a user-specific venv
                    #  2) This test is, generally, intended to be run in a docker container where the system
                    #     python is the correct version that we want to run under.
                    "python",
                    "-c",
                    "import os; print(os.getuid())",
                ]
            )
            # Wait until the process exits.
            while runner.exit_code is None:
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

    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason="Must be running inside of the sudo_environment testing container.",
    )
    @pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
    def test_does_not_inherit_env_vars(
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
                    # Note: Intentionally not `sys.executable`. Reasons:
                    #  1) This is a cross-account command, and sys.executable may be in a user-specific venv
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

    def test_cannot_run_twice(self, tmp_path: Path) -> None:
        # Run a simple command with no timeout and check the state during and
        # after the run.

        # GIVEN
        callback = MagicMock()
        with TerminatingRunner(
            logger=MagicMock(), session_working_directory=tmp_path, callback=callback
        ) as runner:
            # WHEN
            runner._run([sys.executable, "-c", "print('hello')"])

            # THEN
            with pytest.raises(RuntimeError):
                runner._run([sys.executable, "-c", "print('hello')"])

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_run_action(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Run a test of the _run_action method that makes sure that the action runs
        # and the format strings are evaluated.

        # GIVEN
        action = Action_2023_09(
            command="{{Task.PythonInterpreter}}",
            args=["{{Task.ScriptFile}}"],
            timeout=(5 if is_posix() else 15),
        )
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        symtab = SymbolTable(
            source={
                "Task.PythonInterpreter": sys.executable,
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
        assert "Log from test 14" not in messages

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
            command="{{Task.PythonInterpreter}}", args=["{{Task.ScriptFile}}"], timeout=1
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
            runner._run([sys.executable, str(python_app_loc)])

            # WHEN
            runner.cancel()

            # THEN
            # Wait for the app to exit
            while runner.exit_code is None:
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
    ) -> None:
        # Test that the subprocess is terminated when doing a TERMINATE style
        # cancelation

        # GIVEN
        logger = build_logger(queue_handler)
        with TerminatingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()

            # WHEN
            runner._run([sys.executable, str(python_app_loc)], time_limit=timedelta(seconds=1))

            # THEN
            # Wait until the process exits.
            while runner.exit_code is None:
                time.sleep(0.2)
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
    ) -> None:
        # Test that NOTIFY_THEN_CANCEL first signals a SIGTERM and then a SIGKILL

        # GIVEN
        logger = build_logger(queue_handler)
        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (
                Path(__file__).parent / "support_files" / "app_20s_run_ignore_signal.py"
            ).resolve()
            runner._run([sys.executable, str(python_app_loc)])

            # WHEN
            secs = 2 if not is_windows() else 5
            time.sleep(secs)  # Give the process a little time to do something
            now = datetime.utcnow()
            runner.cancel(time_limit=timedelta(seconds=2))

            # THEN
            assert runner.state == ScriptRunnerState.CANCELING
            # Wait until the process exits.
            while runner.exit_code is None:
                time.sleep(0.2)
            # This should be CANCELED rather than TIMEOUT because this test is manually calling
            # the cancel() method rather than letting the action reach its runtime limit.
            assert runner.state == ScriptRunnerState.CANCELED
            assert runner.exit_code != 0
        messages = collect_queue_messages(message_queue)
        assert "Trapped" in messages
        trapped_idx = messages.index("Trapped")
        # Should be at least one more number printed after the Trapped
        # to indicate that we didn't immediately terminate the script.
        assert messages[trapped_idx + 1].isdigit()
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
        time_end = datetime.fromisoformat(notification_data["NotifyEnd"][:-1])
        # Timestamp should be around 2s from cancel signal, but give a 1s window
        # for timing differences.
        delta_t = time_end - now
        assert timedelta(seconds=1) < delta_t < timedelta(seconds=3)

    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_cancel_double_cancel_notify(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that NOTIFY_THEN_CANCEL can be called twice, and the second time will
        # shrink the grace period

        # GIVEN
        logger = build_logger(queue_handler)
        with NotifyingRunner(logger=logger, session_working_directory=tmp_path) as runner:
            python_app_loc = (
                Path(__file__).parent / "support_files" / "app_20s_run_ignore_signal.py"
            ).resolve()
            runner._run([sys.executable, str(python_app_loc)])

            # WHEN
            secs = 2 if not is_windows() else 5
            time.sleep(secs)  # Give the process a little time to do something
            runner.cancel(time_limit=timedelta(seconds=15))
            runner.cancel(time_limit=timedelta(seconds=1 if not is_windows() else 3))

            # THEN
            assert runner.state == ScriptRunnerState.CANCELING
            # Wait until the process exits.
            while runner.exit_code is None:
                time.sleep(0.2)
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
