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
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from logging.handlers import QueueHandler
from queue import SimpleQueue
from typing import Any, Generator, Optional
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions._os_checker import is_posix
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger, create_unique_logger_name, serial_process

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


class _FilterRaisingOnAny(logging.Filter):
    """Raises on any record whose message contains one of ``markers``.

    Separate from :class:`_ExplodingFilter` so the tests using that keep their
    exact single-marker behaviour.

    The raised message names the marker that triggered it, so a test can tell
    *which* log call failed -- which is what distinguishes "the startup line
    raised and was contained" from "something inside the reap raised and replaced
    it". Both are ``RuntimeError`` from the same filter, so nothing else does.
    """

    def __init__(self, *markers: str) -> None:
        super().__init__()
        self._markers = markers
        self.fired = False

    def filter(self, record: logging.LogRecord) -> bool:
        message = str(record.msg)
        for marker in self._markers:
            if marker in message:
                self.fired = True
                raise RuntimeError(f"filter blew up on: {marker}")
        return True


@contextmanager
def logger_carrying(filt: logging.Filter) -> Generator[Any, None, None]:
    """A uniquely-named logger carrying `filt`, removed again on exit.

    The removal is not tidiness: ``logging.getLogger`` interns loggers in a
    process-global registry, so a *raising* filter left installed would outlive
    its test and fire inside an unrelated one.

    Yields ``Any`` rather than ``logging.Logger`` because ``LoggingSubprocess``
    declares its ``logger`` parameter as a ``LoggerAdapter``, while these tests
    pass a plain ``Logger`` -- as the ``exploding_logger`` fixture above already
    does, for the same reason.
    """
    logger = logging.getLogger(create_unique_logger_name(prefix="openjd.test.reaping.startup"))
    logger.setLevel(logging.INFO)
    handler = logging.NullHandler()
    logger.addHandler(handler)
    logger.addFilter(filt)
    try:
        yield logger
    finally:
        logger.removeFilter(filt)
        logger.removeHandler(handler)


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

    def test_a_logging_failure_during_the_reap_does_not_escape(self, python_exe: str) -> None:
        """`_reap`'s own logging must not raise either.

        The containment here already covered the terminate and the wait, but not
        the three `logger.error` calls between them -- and a raising
        `logging.Filter` is exactly what routes execution into `_reap`, which
        makes those calls the *likeliest* thing to fail, not the least. Anything
        escaping replaces the exception already propagating out of `run()`.

        This drives all three call sites at once: the abandoning notice, the
        terminate-failure notice, and the unreaped-after-timeout notice.
        """
        # GIVEN a live child, a termination that fails, a 0s wait budget so the
        # timeout branch is reached too, and a logger that raises on every error
        logger = MagicMock()
        logger.error.side_effect = RuntimeError("the logging stack blew up")
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
                patch("openjd.sessions._subprocess.ABANDONED_PROCESS_REAP_TIMEOUT_SECONDS", 0),
            ):
                # WHEN / THEN: nothing escapes, despite every log call failing
                proc._reap(popen)

            # All three messages were attempted, so this really did exercise
            # every call site rather than returning early.
            assert logger.error.call_count == 3
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


@serial_process
class TestStartupLoggingDoesNotAbandonTheChild:
    """The ownership `try/finally` must begin at process creation, not after the
    startup logging.

    `run()` logged "Command started as pid" and "Output:" *before* entering the
    `try` whose `finally` reaps, and did the POSIX process-group lookup there
    too. Anything raising in that window escaped `run()` having never waited on
    the child, leaving a live process with no exit code recorded while the runner
    published a terminal state for it.

    A `logging.Filter` is the reachable trigger because filters propagate out of
    `Logger.handle`, unlike handlers, whose exceptions go to `handleError` --
    and `Session` installs a filter of its own. The process-group lookup is
    covered by its own test below, which is why the boundary moved to
    immediately after creation rather than merely above the two log calls.
    """

    @pytest.mark.skipif(not is_posix(), reason="pid liveness/reap checks are POSIX-only here")
    def test_a_filter_raising_on_the_pid_line_does_not_leak_the_child(
        self, python_exe: str
    ) -> None:
        # GIVEN: a long-lived child, and a filter that raises on the first
        # startup log line -- while the child is still alive
        exploder = _FilterRaisingOnAny("Command started as pid")
        with logger_carrying(exploder) as logger:
            proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

            # WHEN
            with pytest.raises(RuntimeError, match="blew up"):
                proc.run()

        # THEN: the filter really did fire on that line...
        assert exploder.fired is True
        pid = proc.pid
        assert pid is not None
        # ...the child is gone, not merely signalled...
        assert _wait_gone(pid), f"child {pid} was left running"
        # ...and its status was recorded rather than lost.
        assert proc.exit_code == -signal.SIGKILL  # type: ignore
        assert proc._process is None

    @pytest.mark.skipif(not is_posix(), reason="pid liveness/reap checks are POSIX-only here")
    def test_a_filter_raising_on_the_output_banner_does_not_leak_the_child(
        self, python_exe: str
    ) -> None:
        """A second, separate unprotected line -- not the same one as above."""
        # GIVEN
        exploder = _FilterRaisingOnAny("Output:")
        with logger_carrying(exploder) as logger:
            proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

            # WHEN
            with pytest.raises(RuntimeError, match="blew up"):
                proc.run()

        # THEN
        assert exploder.fired is True
        pid = proc.pid
        # Not decoration: `_wait_gone(None)` returns True, so without this the
        # liveness assertion below degrades to a tautology.
        assert pid is not None
        assert _wait_gone(pid), f"child {pid} was left running"
        assert proc.exit_code == -signal.SIGKILL  # type: ignore
        assert proc._process is None

    @pytest.mark.skipif(not is_posix(), reason="the process-group lookup is POSIX-only")
    def test_a_failing_process_group_lookup_does_not_leak_the_child(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        """The POSIX process-group lookup was in the unguarded window too.

        `ProcessLookupError` from it is caught deliberately (a trivial command
        can exit first), but nothing else was, and it sits between the two log
        lines -- so covering only the logging would leave the middle of the same
        window unprotected. No filter here: the failure is injected into the
        lookup itself, so this is independent of the logging stack.

        What is asserted is only that the reap is *reached*, and deliberately
        NOT that the child dies. When the group lookup is itself what failed,
        `_sudo_child_process_group_id` stays unset, and
        `_posix_signal_subprocess` then declines to signal at all rather than
        target a possibly-recycled pid (the R5-9 "no process group known"
        convention). So on this one path the boundary buys the attempt and the
        handle release, not the kill.

        That is a real, pre-existing limitation of the signalling path -- not
        something this change introduces or claims to fix -- and it is recorded
        as a follow-up. Asserting a dead child here would require standing in for
        the signal, which would make this test assert a kill the shipped code
        does not perform.
        """
        # GIVEN a live child and a process-group lookup that fails in a way the
        # production code does not catch
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

        # WHEN
        with (
            patch(
                "openjd.sessions._subprocess.os.getpgid",
                side_effect=RuntimeError("getpgid blew up"),
            ),
            # Stubbed rather than stood-in-for: the real call would spend a second
            # scanning for a sudo child it will not find, and this test is about
            # whether the reap is reached at all.
            patch.object(
                LoggingSubprocess, "_terminate_process", autospec=True
            ) as terminate_process,
            # No wait budget: the child cannot have been killed by the stub, so
            # the bounded wait would otherwise be spent in full.
            patch("openjd.sessions._subprocess.ABANDONED_PROCESS_REAP_TIMEOUT_SECONDS", 0),
        ):
            with pytest.raises(RuntimeError, match="getpgid blew up"):
                proc.run()

        pid = proc.pid
        try:
            # THEN: ownership was discharged -- the reap ran, instead of the
            # failure escaping in front of it
            terminate_process.assert_called_once()
            assert proc._process is None
        finally:
            # The stubbed terminate killed nothing, so this test owns the child.
            if pid is not None and _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    @pytest.mark.skipif(not is_posix(), reason="pid liveness/reap checks are POSIX-only here")
    def test_the_reap_kills_the_child_even_when_its_own_logging_raises(
        self, python_exe: str
    ) -> None:
        """`_reap` must terminate before it logs.

        With the boundary moved, a raising filter now routes *into* `_reap` -- so
        if that same filter also matches `_reap`'s own "Abandoning the subprocess"
        message, a log-then-terminate order meant the `logger.error` raised and the
        kill never ran. The leak would have survived the boundary fix.

        Two properties, both load-bearing and each failing for a different
        mutation: the child dies (log-then-terminate breaks this), and the caller
        still sees the *startup* failure rather than one raised from inside the
        `finally` (a reap log that propagates breaks this).
        """
        # GIVEN: a filter that raises on the startup line AND on the message
        # `_reap` emits while cleaning up. "Running command" is deliberately not
        # matched, so the child is really created.
        exploder = _FilterRaisingOnAny("Command started as pid", "Abandoning the subprocess")
        with logger_carrying(exploder) as logger:
            proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _LONG_CHILD])

            # WHEN
            with pytest.raises(RuntimeError) as excinfo:
                proc.run()

        # THEN: the child is reaped despite the logging stack being broken...
        pid = proc.pid
        assert pid is not None
        assert _wait_gone(pid), f"child {pid} was left running"
        assert proc.exit_code == -signal.SIGKILL  # type: ignore
        assert proc._process is None
        # ...and the exception is the startup failure. Naming the marker matters:
        # both failures are RuntimeError from the same filter, so only the marker
        # distinguishes "contained" from "replaced the propagating exception".
        assert "filter blew up on: Command started as pid" in str(excinfo.value)
        assert "Abandoning the subprocess" not in str(excinfo.value)

    def test_the_startup_lines_are_still_logged_on_a_healthy_run(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        """Negative control. Deleting the startup logging would satisfy every
        assertion above, so pin that it still happens -- and that a healthy run
        keeps its own exit code."""
        # GIVEN / WHEN
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", _SHORT_CHILD])
        proc.run()

        # THEN
        lines = []
        while not message_queue.empty():
            lines.append(message_queue.get().getMessage())
        assert any(f"Command started as pid: {proc.pid}" in line for line in lines), lines
        assert any(line == "Output:" for line in lines), lines
        assert proc.exit_code == 7
        assert proc.failed_to_start is False


class TestStartFailureAlwaysReleasesWaiters:
    """`_has_started` must be set however `_start_subprocess` returns.

    `_start_subprocess` catches every `Exception` from the launch and logs
    "Process failed to start" -- but that log can itself raise, and then the
    exception propagates out of `run()` before `_has_started.set()` was reached.
    `ScriptRunnerBase._run` waits on `wait_until_started()` with **no timeout**,
    so the thread that called the public `Session` API blocked forever on a
    launch that had already definitively failed.

    Platform-agnostic: no child is ever created (the failing log precedes
    `Popen`), so there is nothing to signal or reap and no POSIX guard is needed.
    """

    def test_a_raising_log_in_the_failure_handler_still_releases_waiters(
        self, python_exe: str
    ) -> None:
        # GIVEN a filter that raises on the pre-launch "Running command" line.
        # The failure handler then logs "Process failed to start: <that error>",
        # whose text contains the marker too, so the handler's own log raises as
        # well -- which is the case that used to escape.
        exploder = _FilterRaisingOnAny("Running command")
        with logger_carrying(exploder) as logger:
            proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", "pass"])

            # WHEN
            with pytest.raises(RuntimeError, match="blew up"):
                proc.run()

        # THEN: waiters are released rather than blocked forever
        assert proc._has_started.is_set() is True
        # No child was created, so nothing leaked.
        assert proc.pid is None

    def test_wait_until_started_returns_after_a_raising_failure_handler(
        self, python_exe: str
    ) -> None:
        """The observable form of the above, through the public API that hung.

        Uses a bounded wait on a daemon thread deliberately: if this regresses,
        the test fails on the flag rather than hanging the suite.
        """
        # GIVEN
        exploder = _FilterRaisingOnAny("Running command")
        with logger_carrying(exploder) as logger:
            proc = LoggingSubprocess(logger=logger, args=[python_exe, "-c", "pass"])
            with pytest.raises(RuntimeError, match="blew up"):
                proc.run()

        returned = threading.Event()

        def waiter() -> None:
            proc.wait_until_started()  # as ScriptRunnerBase._run calls it: no timeout
            returned.set()

        # WHEN
        threading.Thread(target=waiter, daemon=True).start()

        # THEN
        assert returned.wait(timeout=10.0) is True, "wait_until_started() did not return"

    def test_a_normal_start_failure_is_still_reported(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Negative control: the ordinary failure-to-start path is unchanged --
        `_start_subprocess` returns None, `run()` does not raise, and the object
        reports `failed_to_start`."""
        # GIVEN a command that cannot be launched, and a working logger
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(
            logger=logger, args=[str(Path("this-command-does-not-exist-openjd-test"))]
        )

        # WHEN
        proc.run()

        # THEN
        assert proc.failed_to_start is True
        assert proc._has_started.is_set() is True
        assert proc.is_running is False
