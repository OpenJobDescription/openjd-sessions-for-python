# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Regression tests for defects introduced or left open BY the round-5 fix
commit (4c57f40), found by a five-agent sweep of that commit.

The most serious is REG-1: the R5-6 conversion of `ScriptRunnerBase.state`'s
`assert self._run_future is not None` into `return READY` turned a latent
AssertionError into silent state corruption. Two independent reviewers found it.

Every test here was mutation-checked: the fix was reverted and the test was
confirmed to fail. See tmp-free `scripts/` note in the commit message.
"""

import logging
import os
import stat
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions._action_filter import ActionMessageKind, ActionMonitoringFilter
from openjd.sessions._os_checker import is_posix
from openjd.sessions._runner_base import ScriptRunnerBase, ScriptRunnerState
from openjd.sessions._tempdir import OPENJD_TEMPDIR_MODE, custom_gettempdir
from openjd.sessions._types import ActionState


class _Runner(ScriptRunnerBase):
    """Minimal concrete runner."""

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        pass


def _make_record(msg: str, args: Any = None, session_id: str = "foo") -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, "path", 1, msg, args, None)
    record.session_id = session_id  # type: ignore[attr-defined]
    return record


# ===========================================================================
# REG-1 -- the R5-6 `state` conversion must not corrupt the runner
# ===========================================================================


class TestReg1FailedLaunchStateCorruption:
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


# ===========================================================================
# REG-2 -- the R5-2 containment must not itself re-raise
# ===========================================================================


class _UnrenderableError(Exception):
    """An exception whose rendering raises -- the shape that defeated the R5-2
    containment, which interpolated the exception into an f-string."""

    def __str__(self) -> str:
        raise RuntimeError("__str__ is hostile")

    def __repr__(self) -> str:
        raise RuntimeError("__repr__ is hostile too")


class TestReg2ContainmentDoesNotReRaise:
    def test_handler_path_contains_an_unrenderable_exception(self) -> None:
        # GIVEN: a consumer callback raising an exception that cannot be rendered
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise _UnrenderableError()

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        record = _make_record("openjd_progress: 50.0")

        # WHEN / THEN: filter() still returns rather than letting the exception
        # reach the stdout pump thread.
        assert f.filter(record) is True
        assert "_UnrenderableError" in record.getMessage()

    def test_malformed_env_path_contains_an_unrenderable_exception(self) -> None:
        # GIVEN
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise _UnrenderableError()

        f = ActionMonitoringFilter(session_id="foo", callback=callback)

        # WHEN / THEN
        assert f.filter(_make_record("openjd_env : FOO=bar")) is True

    def test_renderable_exceptions_still_report_their_message(self) -> None:
        """The defensive rendering must not degrade the ordinary case."""

        # GIVEN
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise RuntimeError("a perfectly ordinary boom")

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        record = _make_record("openjd_progress: 50.0")

        # WHEN
        f.filter(record)

        # THEN
        assert "a perfectly ordinary boom" in record.getMessage()


# ===========================================================================
# REG-3 -- the R5-3 validation must not be defeatable by a symlink swap
# ===========================================================================


@pytest.mark.skipif(not is_posix(), reason="symlink swap and fchmod semantics are POSIX here")
class TestReg3TempRootCheckThenUse:
    """The first R5-3 implementation validated with `lstat(path)` and then called
    `stat(path)`/`chmod(path)`, both of which re-resolve the name and follow
    links. Swapping the entry for a symlink in between defeated the check and
    chmod'ed the link's target to 0o755."""

    def test_a_symlink_swapped_in_before_the_open_is_refused(self, tmp_path: Path) -> None:
        """Swap during the create window: O_NOFOLLOW must refuse the open."""
        # GIVEN: a victim directory at 0o700 and a parent where the root will live
        victim = tmp_path / "victim"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent"
        parent.mkdir()
        root = parent / "OpenJD"

        real_makedirs = os.makedirs

        def makedirs_then_swap(path: Any, *a: Any, **k: Any) -> None:
            real_makedirs(path, *a, **k)
            if str(path) == str(root):
                os.rmdir(root)
                os.symlink(victim, root, target_is_directory=True)

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.makedirs", side_effect=makedirs_then_swap):
                with pytest.raises(RuntimeError):
                    custom_gettempdir()

        # THEN
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_a_symlink_swapped_in_after_the_open_cannot_redirect_the_chmod(
        self, tmp_path: Path
    ) -> None:
        """Swap *after* the descriptor is open, which O_NOFOLLOW cannot help with.

        This is the case that pins fchmod-vs-chmod. An earlier version of this
        test swapped during the create window instead, where O_NOFOLLOW refuses
        the open before any chmod is attempted -- so it passed even with
        `os.chmod(path)` restored, and pinned nothing about the descriptor. The
        swap is injected from inside `os.fstat`, which the implementation calls
        between the open and the mode change.
        """
        # GIVEN: a victim at 0o700, and a real root whose mode needs correcting
        victim = tmp_path / "victim_after"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent_after"
        parent.mkdir()
        root = parent / "OpenJD"
        root.mkdir()
        os.chmod(root, 0o700)  # != OPENJD_TEMPDIR_MODE, so a mode change is due

        real_fstat = os.fstat
        swapped = {"done": False}

        def fstat_then_swap(fd: int, *a: Any, **k: Any) -> Any:
            result = real_fstat(fd, *a, **k)
            if not swapped["done"]:
                swapped["done"] = True
                os.rmdir(root)
                os.symlink(victim, root, target_is_directory=True)
            return result

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.fstat", side_effect=fstat_then_swap):
                custom_gettempdir()

        # THEN: the swap happened, and the victim was NOT widened. With
        # `os.chmod(temp_dir, ...)` restored this is 0o755.
        assert swapped["done"] is True
        assert os.path.islink(root)
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_the_validated_inode_is_the_one_modified(self, tmp_path: Path) -> None:
        """Even when the swap is not detected as an error, the mode must land on
        the inode that was validated, never on a later resolution of the name."""
        # GIVEN: an existing root at a wrong mode, plus a victim
        victim = tmp_path / "victim2"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent2"
        parent.mkdir()
        root = parent / "OpenJD"
        root.mkdir()
        os.chmod(root, 0o700)  # differs from OPENJD_TEMPDIR_MODE, so a chmod is due

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            result = custom_gettempdir()

        # THEN: the real root got the mode; the victim was not touched.
        assert result == str(root)
        assert stat.S_IMODE(os.stat(root).st_mode) == stat.S_IMODE(OPENJD_TEMPDIR_MODE)
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_a_symlinked_root_is_still_refused(self, tmp_path: Path) -> None:
        """O_NOFOLLOW must keep doing what the explicit S_ISLNK branch did."""
        # GIVEN
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        parent = tmp_path / "parent3"
        parent.mkdir()
        (parent / "OpenJD").symlink_to(elsewhere, target_is_directory=True)

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError, match="real directory"):
                custom_gettempdir()

    def test_no_descriptor_is_leaked(self, tmp_path: Path) -> None:
        """The validation opens a descriptor; every path must close it.

        Probe: the lowest free descriptor number. If a call leaks a descriptor,
        the number the kernel hands out next goes up. Portable across macOS and
        Linux, unlike listing /dev/fd.
        """
        # GIVEN
        parent = tmp_path / "parent4"
        parent.mkdir()
        (parent / "OpenJD").mkdir()

        def lowest_free_fd() -> int:
            fd = os.open(os.devnull, os.O_RDONLY)
            os.close(fd)
            return fd

        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            custom_gettempdir()  # warm up, so first-call allocations do not count
            baseline = lowest_free_fd()

            # WHEN: many successful calls
            for _ in range(25):
                custom_gettempdir()
            after_success = lowest_free_fd()

            # WHEN: many calls that fail *after* the descriptor is opened
            with patch(
                "openjd.sessions._tempdir.os.fstat", side_effect=OSError(5, "induced failure")
            ):
                for _ in range(25):
                    with pytest.raises(OSError):
                        custom_gettempdir()
            after_failure = lowest_free_fd()

        # THEN: no drift on either path.
        assert after_success == baseline
        assert after_failure == baseline
