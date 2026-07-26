# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Runner state around a launch that fails after the subprocess object exists.

``_run``’s single-use guard and the terminal state it publishes both have to hold
when the process/future pair is left inconsistent.
"""

import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions import ActionState
from openjd.sessions._runner_base import ScriptRunnerBase, ScriptRunnerState


class _Runner(ScriptRunnerBase):
    """Minimal concrete runner: only _generate_command_shell_script is exercised."""

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        pass


class TestFailedLaunchDoesNotCorruptRunnerState:
    """A launch that fails after the subprocess object exists must not leave the
    runner looking reusable, and a completed action must always publish a
    terminal state.

    `_run` gates re-entry on `state == READY`. When R5-6 made `state` report
    READY for the (process set, future unset) pair, that guard silently opened.
    And `_on_process_exit` built `ActionState(self.state.value)`, which raises
    `ValueError` for 'ready' -- swallowed by the F8 try/except, so the consumer
    received no terminal callback at all and would wait forever.
    """

    def _runner_with_dead_pool(self, tmp_path: Path, published: list) -> _Runner:
        runner = _Runner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            callback=lambda s: published.append(s.value),
        )
        # Exactly what Session.cleanup() does to the runner's pool. A cleanup
        # racing a launch is the ordinary agent SIGTERM path.
        runner._pool.shutdown(wait=True)
        return runner

    def test_second_launch_is_refused_after_a_failed_launch(self, tmp_path: Path) -> None:
        # GIVEN: a runner whose first launch fails inside _pool.submit
        published: list = []
        runner = self._runner_with_dead_pool(tmp_path, published)
        try:
            with pytest.raises(RuntimeError):
                runner._run([sys.executable, "-c", "pass"], timedelta(seconds=600))

            # WHEN: the runner is handed a working pool and asked to run again,
            # exactly as a caller that caught the RuntimeError might.
            runner._pool = ThreadPoolExecutor(max_workers=1)

            # THEN: the single-use guard holds. Before the fix this launched a
            # second real child and replaced _process, orphaning the first.
            with pytest.raises(RuntimeError, match="cannot be used to run a second subprocess"):
                runner._run([sys.executable, "-c", "import time; time.sleep(30)"], None)
        finally:
            runner.cancel()
            for timer in [t for t in threading.enumerate() if isinstance(t, threading.Timer)]:
                timer.cancel()
            runner.shutdown()

    def test_launch_latch_holds_even_when_state_reports_ready(self, tmp_path: Path) -> None:
        """Pins the guard to `_launched` rather than to the state classification.

        This is the property that actually broke: whatever `state` decides to
        report for an inconsistent runner, re-entry must still be refused.
        """
        # GIVEN: a runner that has attempted a launch, with `state` forced to
        # report READY (the exact misclassification that opened the guard)
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            runner._pool.shutdown(wait=True)
            with pytest.raises(RuntimeError):
                runner._run([sys.executable, "-c", "pass"], None)
            runner._pool = ThreadPoolExecutor(max_workers=1)

            with patch.object(
                type(runner), "state", property(lambda self: ScriptRunnerState.READY)
            ):
                assert runner.state == ScriptRunnerState.READY  # the hostile premise
                # THEN
                with pytest.raises(RuntimeError, match="second subprocess"):
                    runner._run([sys.executable, "-c", "pass"], None)
        finally:
            runner.shutdown()

    def test_completion_always_publishes_a_terminal_state(self, tmp_path: Path) -> None:
        # GIVEN: a runner whose process/future pair is inconsistent, so `state`
        # cannot be classified as a terminal ActionState
        published: list = []
        runner = _Runner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            callback=lambda s: published.append(s),
        )
        try:
            runner._process = MagicMock()
            runner._run_future = None
            completed: Future = Future()
            completed.set_result(None)

            # WHEN
            runner._on_process_exit(completed)

            # THEN: exactly one terminal callback. Before the fix this list was
            # empty: ActionState('ready') raised and the F8 handler ate it.
            assert len(published) == 1
            assert published[0] in (
                ActionState.FAILED,
                ActionState.CANCELED,
                ActionState.TIMEOUT,
                ActionState.SUCCESS,
            )
            assert published[0] == ActionState.FAILED
        finally:
            runner.shutdown()

    def test_terminal_action_state_maps_every_runner_state(self, tmp_path: Path) -> None:
        """No ScriptRunnerState may make the terminal mapping raise."""
        # GIVEN
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            for runner_state in ScriptRunnerState:

                def fixed_state(_self: Any, _s: ScriptRunnerState = runner_state) -> Any:
                    return _s

                # WHEN
                with patch.object(type(runner), "state", property(fixed_state)):
                    result = runner._terminal_action_state()
                # THEN
                assert isinstance(result, ActionState)
        finally:
            runner.shutdown()

    def test_process_and_future_are_published_together(self, tmp_path: Path) -> None:
        """Removes the root cause: a reader must never see a process with no
        future, on the success path or any other."""
        # GIVEN: a poller hammering `state` from another thread during launch
        observations: list[tuple[bool, bool]] = []
        stop = threading.Event()
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)

        def poll() -> None:
            while not stop.is_set():
                observations.append((runner._process is not None, runner._run_future is not None))

        poller = threading.Thread(target=poll, daemon=True)
        try:
            poller.start()
            # WHEN
            runner._run([sys.executable, "-c", "pass"], None)
            stop.set()
            poller.join(timeout=10)

            # THEN: the (process, no future) combination was never observable.
            assert (True, False) not in observations
            assert observations, "the poller must actually have sampled something"
        finally:
            stop.set()
            runner.shutdown()

    def test_a_normal_run_still_reports_success(self, tmp_path: Path) -> None:
        """Guard against the latch or the restructure breaking the happy path."""
        # GIVEN
        published: list = []
        runner = _Runner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            callback=lambda s: published.append(s),
        )
        try:
            # WHEN
            runner._run([sys.executable, "-c", "pass"], None)
            assert runner._run_future is not None
            runner._run_future.result(timeout=30)

            # THEN
            deadline = 30.0
            import time

            start = time.monotonic()
            while ActionState.SUCCESS not in published and time.monotonic() - start < deadline:
                time.sleep(0.02)
            assert runner.state == ScriptRunnerState.SUCCESS
            assert ActionState.SUCCESS in published
        finally:
            runner.shutdown()
