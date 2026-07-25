# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import threading
import time
from datetime import timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Optional, Union
from unittest.mock import MagicMock, patch
import os

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import (
    CancelationMethodNotifyThenTerminate as CancelationMethodNotifyThenTerminate_2023_09,
)
from openjd.model.v2023_09 import (
    CancelationMethodTerminate as CancelationMethodTerminate_2023_09,
)
from openjd.model.v2023_09 import CancelationMode as CancelationMode_2023_09
from openjd.model.v2023_09 import (
    EmbeddedFileText as EmbeddedFileText_2023_09,
)
from openjd.model.v2023_09 import (
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)
from openjd.model.v2023_09 import (
    CommandString as CommandString_2023_09,
    ArgString as ArgString_2023_09,
    DataString as DataString_2023_09,
)
from openjd.model.v2023_09 import StepActions as StepActions_2023_09
from openjd.model.v2023_09 import StepScript as StepScript_2023_09

from openjd.sessions import WindowsSessionUser
from openjd.sessions._runner_base import (
    CancelMethod,
    NotifyCancelMethod,
    ScriptRunnerState,
    TerminateCancelMethod,
)
from openjd.sessions._runner_step_script import StepScriptRunner
from openjd.sessions._subprocess import LoggingSubprocess
from openjd.sessions._tempdir import TempDir
from openjd.sessions._os_checker import is_posix, is_windows

from .conftest import (
    build_logger,
    collect_queue_messages,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
)


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path", "message_queue", "queue_handler")
class TestStepScriptRunner:
    def test_run_basic(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that run of an action with no embedded files works as expected.

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09("{{ Task.Command }}"),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('\"Hello\"')")],
                )
            )
        )
        symtab = SymbolTable(source={"Task.Command": python_exe})
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert '"Hello"' in messages

    def test_run_with_files(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Test that that en embedded file is properly materialized and can be used in the action

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09("{{ Task.Command }}"),
                    args=[ArgString_2023_09("{{ Task.File.Foo }}")],
                )
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data=DataString_2023_09("print('Hello')"),
                )
            ],
        )
        symtab = SymbolTable(source={"Task.Command": python_exe})
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello" in messages
        assert len(symtab.symbols) == 1

    @pytest.mark.parametrize(
        "os_env_vars",
        (
            pytest.param(None, id="No defined env vars"),
            pytest.param({"PATH": os.environ.get("PATH", "")}),
        ),
    )
    def test_run_file_in_session_dir(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        os_env_vars: Optional[dict[str, Optional[str]]],
    ) -> None:
        # Test that if we materialize a script into the session directory, then we can run it by
        # referencing it relative to the Session Working Directory.
        # This primarily is intended to test the locate_windows_executable path of ScriptRunnerBase.

        # GIVEN
        if is_posix():
            script = StepScript_2023_09(
                actions=StepActions_2023_09(
                    onRun=Action_2023_09(command=CommandString_2023_09("./test.sh"))
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        filename="test.sh",
                        runnable=True,
                        data=DataString_2023_09("#!/bin/sh\necho 'Hello!'"),
                    )
                ],
            )
        else:
            script = StepScript_2023_09(
                actions=StepActions_2023_09(
                    onRun=Action_2023_09(command=CommandString_2023_09("test.bat"))
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        filename="test.bat",
                        data=DataString_2023_09("echo Hello!"),
                    )
                ],
            )
        symtab = SymbolTable()
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
            os_env_vars=os_env_vars,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello!" in messages

    @pytest.mark.parametrize(
        "cancel_method,expected",
        [
            pytest.param(None, TerminateCancelMethod(), id="default is terminate"),
            pytest.param(
                CancelationMethodTerminate_2023_09(mode=CancelationMode_2023_09.TERMINATE),
                TerminateCancelMethod(),
                id="terminate is terminate",
            ),
            pytest.param(
                CancelationMethodNotifyThenTerminate_2023_09(
                    mode=CancelationMode_2023_09.NOTIFY_THEN_TERMINATE
                ),
                NotifyCancelMethod(terminate_delay=timedelta(seconds=120)),
                id="default notify period is 120s",
            ),
            pytest.param(
                CancelationMethodNotifyThenTerminate_2023_09(
                    mode=CancelationMode_2023_09.NOTIFY_THEN_TERMINATE, notifyPeriodInSeconds=10
                ),
                NotifyCancelMethod(terminate_delay=timedelta(seconds=10)),
                id="uses notify period",
            ),
        ],
    )
    def test_cancel(
        self,
        tmp_path: Path,
        cancel_method: Optional[
            Union[
                CancelationMethodNotifyThenTerminate_2023_09,
                CancelationMethodTerminate_2023_09,
            ]
        ],
        expected: CancelMethod,
        python_exe: str,
    ) -> None:
        # Test that cancel invokes the base class' cancel with the appropriate arguments.

        # We'll do this one with mocks to avoid timing/race-condition issues.
        # The lower-level process runners have been thoroughly tested for cancel's
        # functionality, so this seems fine.

        # Patch _run (not _run_action): the effective cancel method is now
        # resolved by _run_action at launch time and consumed by cancel().
        with patch.object(StepScriptRunner, "_run"):
            with patch.object(StepScriptRunner, "_cancel") as mock_cancel:
                # GIVEN
                script = StepScript_2023_09(
                    actions=StepActions_2023_09(
                        onRun=Action_2023_09(
                            command=CommandString_2023_09("{{ Task.Command }}"),
                            args=[ArgString_2023_09("-c"), ArgString_2023_09("print('Hello')")],
                            cancelation=cancel_method,
                        )
                    )
                )
                symtab = SymbolTable(source={"Task.Command": python_exe})
                runner = StepScriptRunner(
                    logger=MagicMock(),
                    session_working_directory=tmp_path,
                    script=script,
                    symtab=symtab,
                    session_files_directory=tmp_path,
                )
                runner.run()
                # _run is patched out, so stand in for the subprocess it would have
                # created: a cancel that arrives before one exists is deferred
                # rather than applied (see ScriptRunnerBase._pending_cancel).
                runner._process = MagicMock()
                time_limit = timedelta(30)

                # WHEN
                runner.cancel(time_limit=time_limit)

                # THEN
                arg0 = mock_cancel.call_args.args[0]
                assert arg0 == expected
                arg1 = mock_cancel.call_args.args[1]
                assert arg1 is time_limit

    @pytest.mark.timeout(60)  # GitHub CI file operations may be causing a timeout
    @pytest.mark.skipif(not is_windows(), reason="Windows-only test")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    @pytest.mark.parametrize(
        "os_env_vars",
        (
            pytest.param(None, id="No defined env vars"),
            pytest.param({"PATH": os.environ.get("PATH", "")}),
        ),
    )
    def test_run_file_in_session_dir_as_windows_user(
        self,
        windows_user: WindowsSessionUser,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        os_env_vars: Optional[dict[str, Optional[str]]],
    ) -> None:
        # Test that if we materialize a script into the session directory, then we can run it by
        # referencing it relative to the Session Working Directory.
        # This primarily is intended to test the locate_windows_executable path of ScriptRunnerBase.

        # GIVEN
        tmpdir = TempDir(user=windows_user)
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=CommandString_2023_09(r"test.bat"))
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    filename="test.bat",
                    data=DataString_2023_09("echo Hello!"),
                )
            ],
        )
        symtab = SymbolTable()
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmpdir.path,
            script=script,
            symtab=symtab,
            session_files_directory=tmpdir.path,
            os_env_vars=os_env_vars,
            user=windows_user,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        tmpdir.cleanup()

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello!" in messages


class TestCancelRacingLaunch:
    """A cancel is delivered from another thread, so it can land while the action
    is still being set up -- after the effective cancel method has been resolved
    but before the subprocess exists. It must be remembered and applied, not
    dropped and not raised (openjd-rs holds the equivalent state in a sticky
    CancellationToken)."""

    def _runner(self, tmp_path: Path, python_exe: str) -> StepScriptRunner:
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('hi')")],
                )
            )
        )
        return StepScriptRunner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            script=script,
            symtab=SymbolTable(),
            session_files_directory=tmp_path,
        )

    def test_cancel_before_subprocess_is_remembered(self, tmp_path: Path, python_exe: str) -> None:
        # GIVEN: the action resolved (so a cancel method is stored) but _run was
        # patched out, so no subprocess exists yet
        with patch.object(StepScriptRunner, "_run"):
            runner = self._runner(tmp_path, python_exe)
            runner.run()
        assert runner._process is None
        assert runner._resolved_cancel_method == TerminateCancelMethod()

        # WHEN: a cancel lands in that window
        runner.cancel(time_limit=timedelta(seconds=7))

        # THEN: it is remembered rather than dropped, and nothing was raised
        assert runner._pending_cancel == (timedelta(seconds=7), False)

    def test_pending_cancel_is_applied_once_the_subprocess_exists(
        self, tmp_path: Path, python_exe: str
    ) -> None:
        # GIVEN: a cancel recorded during setup
        with patch.object(StepScriptRunner, "_run"):
            runner = self._runner(tmp_path, python_exe)
            runner.run()
        runner.cancel(mark_action_failed=True)
        assert runner._pending_cancel == (None, True)

        # WHEN: the real _run finishes creating the subprocess
        with patch.object(StepScriptRunner, "_cancel") as mock_cancel:
            runner._process = MagicMock()
            pending = runner._pending_cancel
            assert pending is not None
            runner._pending_cancel = None
            assert runner._resolved_cancel_method is not None
            runner._cancel(runner._resolved_cancel_method, *pending)

        # THEN: the stored method is delivered with the recorded arguments
        assert mock_cancel.call_args.args[0] == TerminateCancelMethod()
        assert mock_cancel.call_args.args[2] is True

    def test_cancel_with_no_subprocess_does_not_raise(
        self, tmp_path: Path, python_exe: str
    ) -> None:
        # GIVEN: a runner that never launched anything
        runner = self._runner(tmp_path, python_exe)

        # WHEN / THEN: the low-level cancel is a quiet no-op, not an AssertionError
        runner._cancel(TerminateCancelMethod())


class TestCancelRacingLaunchIsSerialized:
    """The pending-cancel handoff must be atomic with respect to launch.

    `cancel()` runs on another thread. Without serialization there is an
    interleaving where the canceller sees a subprocess object and hands off to
    `_cancel`, which has nothing to signal because the process has not started
    yet, while `_run` has already passed the point where it consumes a pending
    cancel -- so the cancel is dropped by both sides and the action runs to
    completion.
    """

    @pytest.mark.skipif(not is_posix(), reason="signal delivery is posix-only here")
    @pytest.mark.timeout(120)
    def test_cancel_during_launch_is_not_dropped(self, tmp_path: Path, python_exe: str) -> None:
        # GIVEN: a long-running action, and a canceller that fires while the
        # subprocess object exists but has NOT started -- held open by blocking
        # inside _start_subprocess, which runs on the pool thread after _run has
        # already assigned self._process.
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[
                        ArgString_2023_09("-c"),
                        ArgString_2023_09("import time; time.sleep(30)"),
                    ],
                )
            )
        )
        runner = StepScriptRunner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            script=script,
            symtab=SymbolTable(),
            session_files_directory=tmp_path,
        )
        in_window = threading.Event()
        canceller_done = threading.Event()
        real_start = LoggingSubprocess._start_subprocess

        def _start_after_cancel(subproc):  # type: ignore[no-untyped-def]
            in_window.set()
            # Hold the window: _has_started is not set until this returns.
            canceller_done.wait(timeout=30)
            return real_start(subproc)

        def _cancel_in_window() -> None:
            in_window.wait(timeout=30)
            assert runner._process is not None
            assert runner._process.has_started is False
            runner.cancel()
            canceller_done.set()

        canceller = threading.Thread(target=_cancel_in_window, daemon=True)

        # WHEN
        with patch.object(LoggingSubprocess, "_start_subprocess", _start_after_cancel):
            canceller.start()
            runner.run()
        canceller.join(timeout=30)

        # THEN: the cancel was applied, so the 30 second sleep did not run to
        # completion. Unsynchronized, it is dropped by both sides and the runner
        # ends in SUCCESS after 30 seconds.
        deadline = time.time() + 60
        while runner.state in (ScriptRunnerState.RUNNING, ScriptRunnerState.CANCELING):
            if time.time() > deadline:  # pragma: no cover - timing guard
                pytest.fail(f"runner never settled; state={runner.state}")
            time.sleep(0.05)
        assert runner.state == ScriptRunnerState.CANCELED
        assert runner._pending_cancel is None
