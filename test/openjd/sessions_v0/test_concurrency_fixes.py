# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for concurrency fixes F1-F8 from PR #333 review findings.

These tests verify the defensive behaviors added to close race windows.
True concurrency races are inherently non-deterministic, so these tests
focus on verifying the code paths and edge-case handling rather than
timing-dependent race reproduction.
"""

import sys
import threading
from datetime import timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from unittest.mock import MagicMock, patch

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    EnvironmentActions as EnvironmentActions_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
)
from openjd.sessions import ActionState
from openjd.sessions._runner_env_script import EnvironmentScriptRunner
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger
from .conftest import serial_process


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestF1EnvScriptCancelDuringSetup:
    """F1: Cancel during environment script setup must be recorded as pending."""

    def test_cancel_before_action_assignment_records_pending(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify cancel() before _action is assigned records pending cancel."""
        # GIVEN: An EnvironmentScriptRunner with no _action yet
        logger = build_logger(queue_handler)
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # Verify _action is None (before enter() or exit() is called)
        assert runner._action is None

        # WHEN: cancel() is called before _action is set
        runner.cancel(time_limit=timedelta(seconds=5), mark_action_failed=True)

        # THEN: The cancel is recorded as pending (not silently dropped)
        assert runner._pending_cancel == (timedelta(seconds=5), True)

        runner.shutdown()


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestF3MonotonicMergePendingCancels:
    """F3: Duplicate pending cancels must merge monotonically."""

    def test_pending_cancel_merge_takes_minimum_time_limit(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify multiple pending cancels merge with min(time_limit)."""
        logger = build_logger(queue_handler)
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN: First cancel with 10 second limit
        runner.cancel(time_limit=timedelta(seconds=10), mark_action_failed=False)
        assert runner._pending_cancel == (timedelta(seconds=10), False)

        # AND: Second cancel with 5 second limit (tighter)
        runner.cancel(time_limit=timedelta(seconds=5), mark_action_failed=False)

        # THEN: Merged result takes minimum time limit
        assert runner._pending_cancel == (timedelta(seconds=5), False)

        runner.shutdown()

    def test_pending_cancel_merge_or_mark_action_failed(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify multiple pending cancels OR the mark_action_failed flags."""
        logger = build_logger(queue_handler)
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN: First cancel without mark_action_failed
        runner.cancel(time_limit=timedelta(seconds=10), mark_action_failed=False)
        assert runner._pending_cancel == (timedelta(seconds=10), False)

        # AND: Second cancel with mark_action_failed=True
        runner.cancel(time_limit=timedelta(seconds=15), mark_action_failed=True)

        # THEN: Merged result ORs the failed flags (once failed, always failed)
        # and takes the minimum time limit
        assert runner._pending_cancel == (timedelta(seconds=10), True)

        runner.shutdown()

    def test_pending_cancel_merge_none_beats_unlimited(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify that a defined limit beats None (unlimited)."""
        logger = build_logger(queue_handler)
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN: First cancel with unlimited time (None)
        runner.cancel(time_limit=None, mark_action_failed=False)
        assert runner._pending_cancel == (None, False)

        # AND: Second cancel with defined limit
        runner.cancel(time_limit=timedelta(seconds=5), mark_action_failed=False)

        # THEN: Defined limit wins over unlimited
        assert runner._pending_cancel == (timedelta(seconds=5), False)

        runner.shutdown()


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestF7SelfJoinDetection:
    """F7: shutdown() must not deadlock when called from worker thread."""

    def test_shutdown_from_worker_thread_uses_wait_false(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify shutdown() detects self-join and uses wait=False."""
        logger = build_logger(queue_handler)
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # Simulate calling shutdown from the pool's worker thread
        # by adding current thread to _threads set
        threads = runner._pool._threads
        if isinstance(threads, set):
            threads.add(threading.current_thread())

        # WHEN: shutdown() is called (would deadlock if wait=True)
        with patch.object(runner._pool, "shutdown") as mock_shutdown:
            runner.shutdown()

            # THEN: shutdown was called with wait=False
            mock_shutdown.assert_called_once_with(wait=False)


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestF8ObserverExceptionHandling:
    """F8: Observer callback exceptions must not discard live children."""

    def test_callback_exception_is_caught_and_logged(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        """Verify callback exceptions don't propagate from _on_process_exit."""
        logger = build_logger(queue_handler)

        # Create a callback that raises an exception
        def bad_callback(state: ActionState) -> None:
            raise RuntimeError("Observer exploded!")

        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("-c"), ArgString_2023_09("print('test')")],
                )
            )
        )
        symtab = SymbolTable(source={})

        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
            callback=bad_callback,
        )

        # Simulate the state after a process has run
        runner._process = MagicMock()
        runner._process.is_running = False
        mock_future = MagicMock()
        mock_future.exception.return_value = None
        runner._run_future = mock_future

        # WHEN: _on_process_exit is called (with bad callback)
        # THEN: No exception should propagate
        runner._on_process_exit(mock_future)

        # The runner should still be in a consistent state
        runner.shutdown()


@pytest.mark.usefixtures("message_queue", "queue_handler")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-specific signal handling")
@serial_process
class TestReview22F4DoubleLoadFix:
    """Review22-F4: notify/terminate must bind _process once and pass to helpers."""

    def test_notify_binds_process_once(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """Verify notify() binds _process once and passes to helper (R4-G8 fix)."""
        logger = build_logger(queue_handler)

        subprocess = LoggingSubprocess(
            logger=logger,
            args=["true"],
            working_dir=str(tmp_path),
        )

        # Set up a mock process
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process is running
        subprocess._process = mock_process

        # WHEN: notify() is called
        with patch.object(subprocess, "_posix_signal_subprocess") as mock_signal:
            subprocess.notify()

            # THEN: Signal helper was called with the bound process (R4-G8)
            mock_signal.assert_called_once_with(mock_process, signal_name="term")

    def test_terminate_binds_process_once(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """Verify terminate() binds _process once and passes to helper (R4-G8 fix)."""
        logger = build_logger(queue_handler)

        subprocess = LoggingSubprocess(
            logger=logger,
            args=["true"],
            working_dir=str(tmp_path),
        )

        # Set up a mock process
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Process is running
        subprocess._process = mock_process

        # WHEN: terminate() is called
        with patch.object(subprocess, "_posix_signal_subprocess") as mock_signal:
            subprocess.terminate()

            # THEN: Signal helper was called with the bound process (R4-G8)
            mock_signal.assert_called_once_with(mock_process, signal_name="kill")
