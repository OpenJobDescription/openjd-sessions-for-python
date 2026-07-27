# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""``LoggingSubprocess.run()`` owns its child on every exit path.

``run()`` is the only place that holds the ``Popen`` handle, and clearing
``self._process`` in its ``finally`` is the point after which nothing can reach
the child again -- ``notify()`` and ``terminate()`` both become permanent no-ops.
So ownership has to be discharged before that, on every path, not only on the one
that reaches ``wait()``.

It was not. ``_log_subproc_stdout()``, ``wait()`` and the returncode capture all
sat in one ``try``, so any exception out of the stdout pump skipped the wait and
the capture while still clearing the handle. The result was a live child with no
owner, no exit code, and an object reporting neither ``is_running`` nor
``failed_to_start``.

The trigger is a ``logging.Filter`` that raises. Filters run inside
``Logger.handle``, so they propagate into the pump, and installing one is a
supported use of this library -- ``Session`` installs its own
(``ActionMonitoringFilter``). Hardening that one filter, as an earlier change did,
does not close the structural hole here.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from logging.handlers import QueueHandler
from queue import SimpleQueue
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions._os_checker import is_posix
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger

# A child that emits one line (to drive the pump), then outlives the pump.
_LONG_CHILD = "import time\nprint('MARKER', flush=True)\ntime.sleep(60)\n"
# A child that emits one line and exits promptly with a known code.
_SHORT_CHILD = "import sys\nprint('MARKER', flush=True)\nsys.exit(7)\n"


class _ExplodingFilter(logging.Filter):
    """Raises when the child's output passes through it."""

    def __init__(self, marker: str = "MARKER") -> None:
        super().__init__()
        self._marker = marker
        self.fired = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self._marker in str(record.msg):
            self.fired = True
            raise RuntimeError("a log filter blew up while forwarding child output")
        return True


def _pid_alive(pid: int) -> bool:
    """Is `pid` a process we can still signal?

    On POSIX a reaped child is gone from the table, so ``kill(pid, 0)`` raising
    ESRCH is the reap check. A *zombie* would still answer signal 0, so this
    distinguishes "terminated and reaped" from "terminated and leaked".
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid: Optional[int], timeout_s: float = 15.0) -> bool:
    if pid is None:
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def exploding_logger() -> Any:
    """A logger whose filter raises on the child's output, plus the filter."""
    exploder = _ExplodingFilter()
    logger = logging.getLogger(f"openjd.test.reaping.{id(exploder)}")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.NullHandler())
    logger.addFilter(exploder)
    try:
        yield logger, exploder
    finally:
        logger.removeFilter(exploder)


@pytest.mark.skipif(not is_posix(), reason="pid liveness/reap checks are POSIX-only here")
class TestPumpExceptionDoesNotAbandonTheChild:
    def test_live_child_is_terminated_and_reaped(
        self, exploding_logger: Any, python_exe: str
    ) -> None:
        # GIVEN: a long-running child and a log filter that will raise on its
        # first line of output, while the child is still alive
        logger, exploder = exploding_logger
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

        # WHEN
        with pytest.raises(RuntimeError, match="blew up"):
            proc.run()

        # THEN: the filter really did fire mid-pump (otherwise this test proves
        # nothing about the abandonment path)...
        assert exploder.fired is True
        pid = proc.pid
        assert pid is not None
        # ...and the child is gone, not merely signalled.
        assert _wait_gone(pid), f"child {pid} was left running"

    def test_exit_code_is_recorded_for_an_abandoned_child(
        self, exploding_logger: Any, python_exe: str
    ) -> None:
        """The exit code is the only evidence of what happened to the action, and
        it used to be lost entirely."""
        # GIVEN
        logger, _ = exploding_logger
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

        # WHEN
        with pytest.raises(RuntimeError):
            proc.run()

        # THEN: a real exit status, not None. SIGKILL shows as -SIGKILL.
        assert proc.exit_code is not None
        assert proc.exit_code == -signal.SIGKILL  # type: ignore

    def test_a_child_that_exited_on_its_own_keeps_its_own_exit_code(
        self, exploding_logger: Any, python_exe: str
    ) -> None:
        """The reap must not overwrite a genuine exit status with a kill signal."""
        # GIVEN: a child that exits 7 by itself, and a filter that raises
        logger, _ = exploding_logger
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _SHORT_CHILD])

        # WHEN
        with pytest.raises(RuntimeError):
            proc.run()

        # THEN: whichever way the race went, the recorded code is a real one and
        # never None. If the child had already exited, it must be its own 7.
        assert proc.exit_code is not None
        assert proc.exit_code in (7, -signal.SIGKILL)  # type: ignore

    def test_a_reap_failure_does_not_replace_the_original_exception(
        self, exploding_logger: Any, python_exe: str
    ) -> None:
        """The reap runs inside `run()`'s `finally`, so anything it raises would
        replace the exception already propagating -- hiding the real cause.

        Goes through `run()` rather than calling `_reap` directly: the
        containment being pinned is the `finally`'s, and a test that calls
        `_reap` itself never enters that `finally` at all. An earlier version of
        this file made that mistake, and narrowing the reap's `except Exception`
        to `except OSError` passed every test.

        The injected failure is deliberately NOT an OSError, so a handler that
        only catches OSError lets it through.
        """
        # GIVEN: a live child, a filter that raises, and a termination that fails
        # with a non-OSError
        logger, exploder = exploding_logger
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

        # WHEN
        with (
            patch.object(
                LoggingSubprocess,
                "_terminate_process",
                side_effect=RuntimeError("terminate itself blew up"),
            ),
            patch("openjd.sessions._subprocess.ABANDONED_PROCESS_REAP_TIMEOUT_SECONDS", 0),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                proc.run()

        # THEN: the caller sees the pump's failure, which is the real cause -- not
        # the reap's.
        assert exploder.fired is True
        assert "blew up while forwarding child output" in str(excinfo.value)
        assert "terminate itself blew up" not in str(excinfo.value)
        # ...and the handle is still released.
        assert proc._process is None

        # Clean up the child the failed terminate left behind.
        if proc.pid is not None and _pid_alive(proc.pid):
            os.kill(proc.pid, signal.SIGKILL)  # type: ignore
            try:
                os.waitpid(proc.pid, 0)
            except ChildProcessError:
                # Already reaped -- which is the outcome this test wants anyway.
                # Popen's own destructor or the code under test may have got there
                # first; either way there is no zombie left to collect.
                pass

    def test_the_process_handle_is_still_released(
        self, exploding_logger: Any, python_exe: str
    ) -> None:
        """Reaping must not come at the cost of the deallocation the `finally`
        existed for."""
        # GIVEN / WHEN
        logger, _ = exploding_logger
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])
        with pytest.raises(RuntimeError):
            proc.run()

        # THEN
        assert proc._process is None
        assert proc.is_running is False


@pytest.mark.skipif(not is_posix(), reason="POSIX signal semantics")
class TestNormalPathIsUnchanged:
    """The reap is a no-op when `run()` completed properly. These are the tests
    that would catch it terminating a healthy child or clobbering its status."""

    def test_successful_run_keeps_its_exit_code_and_is_not_signalled(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _SHORT_CHILD])

        # WHEN
        proc.run()

        # THEN: its own exit code, positive -- not a negative signal value.
        assert proc.exit_code == 7
        assert proc.failed_to_start is False

    def test_successful_run_does_not_terminate_the_child(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        """Directly pins that the new path does not fire on a healthy run."""
        # GIVEN
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", "pass"])

        # WHEN
        with patch.object(LoggingSubprocess, "_terminate_process", autospec=True) as terminate:
            proc.run()

        # THEN
        terminate.assert_not_called()
        assert proc.exit_code == 0

    def test_output_is_still_forwarded(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        # GIVEN / WHEN
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _SHORT_CHILD])
        proc.run()

        # THEN
        lines = []
        while not message_queue.empty():
            lines.append(message_queue.get().getMessage())
        assert "MARKER" in lines


@pytest.mark.skipif(not is_posix(), reason="POSIX signal semantics")
class TestReapUnitBehaviour:
    """Direct coverage of `_reap` for the branches the end-to-end tests cannot
    reach cheaply."""

    def test_reap_of_none_is_a_no_op(self) -> None:
        # GIVEN a subprocess that never started
        proc = LoggingSubprocess(logger=MagicMock(), args=["/bin/echo", "hi"])

        # WHEN / THEN: no exception, and no exit code invented
        proc._reap(None)
        assert proc.exit_code is None

    def test_a_terminate_failure_does_not_escape(self, python_exe: str) -> None:
        """`_reap` runs inside a `finally`. An exception from it would replace
        whatever exception was already propagating."""
        # GIVEN a live child whose termination will fail
        logger = MagicMock()
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])
        popen = proc._start_subprocess()
        assert popen is not None
        try:
            with (
                patch.object(
                    LoggingSubprocess,
                    "_terminate_process",
                    side_effect=OSError("cannot signal"),
                ),
                patch(
                    # The child survives, since termination was made to fail, so the
                    # wait that follows must not burn its real budget here.
                    "openjd.sessions._subprocess.ABANDONED_PROCESS_REAP_TIMEOUT_SECONDS",
                    0,
                ),
            ):
                # WHEN / THEN: swallowed and logged, not raised
                proc._reap(popen)
            assert any("Could not terminate" in str(call) for call in logger.error.call_args_list)
        finally:
            popen.kill()
            popen.wait()

    def test_a_child_that_survives_termination_is_reported_not_hung(self, python_exe: str) -> None:
        """The wait is bounded: a child that somehow outlives SIGKILL must not
        hang the pool worker."""
        # GIVEN a live child, a termination that does nothing, and a 0s budget
        logger = MagicMock()
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])
        popen = proc._start_subprocess()
        assert popen is not None
        try:
            with (
                patch.object(LoggingSubprocess, "_terminate_process"),
                patch("openjd.sessions._subprocess.ABANDONED_PROCESS_REAP_TIMEOUT_SECONDS", 0),
            ):
                start = time.monotonic()
                # WHEN
                proc._reap(popen)
                elapsed = time.monotonic() - start

            # THEN: returned promptly, and said so.
            assert elapsed < 10.0
            assert any("did not exit within" in str(call) for call in logger.error.call_args_list)
        finally:
            popen.kill()
            popen.wait()


class TestTerminateDoesNotDependOnLeaderLiveness:
    """`terminate()` must still signal after the group leader has exited.

    The signal target is a process *group*, and killpg still reaches surviving
    members once the leader has exited and been reaped. Gating the call on
    `proc.poll() is None` made it a no-op in exactly the case where the recorded
    group was the only remaining handle on those survivors -- which is the shape
    a wrap environment produces, a wrapper that execs and returns while its
    workload lives on. The children then outlived terminal publication.

    openjd-rs never gates: every cancel/timeout/reap-failure path calls
    `send_terminate(pid)` -> killpg unconditionally, and its `is_process_alive()`
    probe is `#[allow(dead_code)]` for this reason.
    """

    def test_signal_is_sent_even_when_poll_reports_the_leader_gone(self) -> None:
        # GIVEN a subprocess whose leader has already exited
        subproc = LoggingSubprocess(logger=MagicMock(), args=["unused"])
        departed = MagicMock()
        departed.poll.return_value = 0  # reaped; a live workload may remain
        subproc._process = departed

        # WHEN
        with patch.object(subproc, "_terminate_process") as terminate_process:
            subproc.terminate()

        # THEN: the group is still signalled
        terminate_process.assert_called_once_with(departed)

    def test_nothing_is_signalled_once_the_process_is_released(self) -> None:
        """Negative control: `run()`'s finally clears `_process` after reaping,
        and terminate() must stay a no-op from then on -- otherwise the pid
        could be recycled and an unrelated group signalled."""
        # GIVEN a subprocess that has released its handle
        subproc = LoggingSubprocess(logger=MagicMock(), args=["unused"])
        subproc._process = None

        # WHEN
        with patch.object(subproc, "_terminate_process") as terminate_process:
            subproc.terminate()

        # THEN
        terminate_process.assert_not_called()
