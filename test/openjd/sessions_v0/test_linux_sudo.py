# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import shutil
import signal
import subprocess
import time
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from subprocess import DEVNULL, PIPE, CompletedProcess, Popen
from typing import Callable, Generator, Optional
from unittest.mock import Mock, patch

import pytest

from openjd.sessions._linux._sudo import (
    FindSignalTargetError,
    find_child_process_id_pgrep,
    find_sudo_child_process_group_id,
)
from openjd.sessions._os_checker import is_posix

from .conftest import build_logger, collect_queue_messages

# pgrep's documented exit status for "no processes matched". Spelled out as a
# literal rather than imported from the module under test: a test that reused the
# module's own constant could not tell us whether that constant matches what the
# operating system actually returns.
PGREP_EXIT_NO_MATCH = 1

# The stand-in for sudo: a process that gains its child only after a delay, so a
# scan that starts immediately is guaranteed to see no children on its first look.
PARENT_SCRIPT = """
import subprocess
import sys
import time

python_exe, child_script, pgid_file, delay_seconds = sys.argv[1:5]
time.sleep(float(delay_seconds))
# start_new_session, as sudo does, so the child lands in its own process group.
child = subprocess.Popen([python_exe, child_script, pgid_file], start_new_session=True)
child.wait()
"""

# The stand-in for the workload. It reports its own process group id, so the test
# asserts against the group the kernel actually assigned rather than recomputing
# it the same way the code under test does.
CHILD_SCRIPT = """
import os
import sys
import time

pgid_file = sys.argv[1]
# Written to a temporary name and renamed, so a reader never sees a partial id.
tmp_file = pgid_file + ".tmp"
with open(tmp_file, "w") as f:
    f.write(str(os.getpgid(0)))
os.replace(tmp_file, pgid_file)
time.sleep(15)
"""


# A process that moves into its own process group only after a delay, reproducing
# the window sudo leaves open: it forks first and creates the new process group
# second, so a scan can legitimately observe the child still sharing sudo's group.
#
# setsid() rather than setpgid(): the process is started without
# start_new_session, so it is not already a group leader and setsid() cannot fail
# with EPERM. It reports the group it ends up in, so the test asserts against what
# the kernel assigned rather than recomputing it.
LATE_PROCESS_GROUP_SCRIPT = """
import os
import time

pgid_file = %(pgid_file)s
time.sleep(%(delay_seconds)s)
os.setsid()
tmp_file = pgid_file + ".tmp"
with open(tmp_file, "w") as f:
    f.write(str(os.getpgid(0)))
os.replace(tmp_file, pgid_file)
time.sleep(15)
"""


def pgrep_result(returncode: int, stdout: str = "") -> CompletedProcess:
    """A stand-in for the CompletedProcess of a `pgrep -P <pid>` run."""
    return CompletedProcess(args=["pgrep", "-P", "1234"], returncode=returncode, stdout=stdout)


@pytest.fixture
def sleeper(python_exe: str) -> Generator[Callable[..., Popen], None, None]:
    """Factory for live, long-lived child processes, all killed and reaped at teardown.

    Real processes rather than fabricated pids because the code under test calls
    os.getpgid() on whatever the scan returns: a made-up pid would either raise
    ProcessLookupError or -- worse, if the number happened to be live -- report
    the process group of something unrelated.

    Registration happens inside the factory, so a process cannot be leaked by a
    failure between starting it and the caller's first statement.
    """
    procs: list[Popen] = []

    def start(*, own_process_group: bool = False, script: Optional[str] = None) -> Popen:
        argv = [python_exe, "-c", script if script else "import time; time.sleep(30)"]
        proc = Popen(
            argv,
            stdin=DEVNULL,
            stdout=DEVNULL,
            # A scripted child can die on startup, and without its stderr the test
            # that depends on it fails with no explanation. The plain sleeper
            # cannot fail that way, so it keeps the pipe-free form.
            stderr=PIPE if script else DEVNULL,
            # start_new_session is what sudo does: the child becomes a session and
            # process-group leader, so its pgid is its own pid and is knowable
            # without asking the code under test.
            start_new_session=own_process_group,
        )
        procs.append(proc)
        return proc

    yield start

    for proc in procs:
        # Popen.kill() is a no-op once the process has been reaped, so this is safe
        # for the already-reaped process the short-circuit test needs.
        proc.kill()
        try:
            # communicate() rather than wait(): it drains the stderr pipe as well
            # as reaping, so a child that wrote to stderr cannot wedge teardown.
            proc.communicate(timeout=10)
        except ValueError:
            # A test on its failure path already read and closed that pipe.
            proc.wait(timeout=10)


def stderr_of(proc: Popen) -> bytes:
    """The child's stderr, for the diagnostic on a failing assertion.

    Killed first: this is only reached when the child has already failed to do
    what a test needed of it, and a still-sleeping child would otherwise hold
    communicate() open for the rest of its life.
    """
    proc.kill()
    return proc.communicate(timeout=10)[1] or b""


def poll_for_pgid(pgid_file: Path, timeout_seconds: float) -> Optional[int]:
    """The process group id reported by the child, or None if it never reported.

    Polled rather than slept on, and a None return makes the caller's assertion
    fail loudly instead of the test quietly passing on a missing file. The file
    is always read at least once, so a zero timeout still answers.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return int(pgid_file.read_text())
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


@pytest.mark.skipif(not is_posix(), reason="pgrep and process groups are posix-only")
class TestFindChildProcessIdPgrep:
    """Tests for find_child_process_id_pgrep(), the non-Linux POSIX lookup of
    sudo's child process.

    Defect pinned: this function raised FindSignalTargetError for *any* non-zero
    pgrep exit. Exit 1 means "no processes matched", which is the expected answer
    for most of the caller's retry window -- sudo has forked but the kernel has
    not finished creating the workload yet. Because the caller's
    `except FindSignalTargetError` sits outside its retry loop, that first empty
    poll ended all retries, so on every non-Linux POSIX host a cross-user launch
    recorded no process group and a later cancel had nothing to signal.
    """

    def test_no_matching_process_returns_none(self, python_exe: str) -> None:
        """Exit 1 is an answer ("none yet"), not a failure.

        Run against a real, live process that has no children of its own, so it
        exercises the real pgrep rather than an assumption about it. The probe
        below asserts the operating system's half of the contract -- that this
        situation really is exit 1 with no output -- and the `result is None`
        assertion is what fails if exit 1 raises again.
        """
        # GIVEN a live process with no children of its own
        proc = Popen(
            [python_exe, "-c", "import time; time.sleep(15)"],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        )
        try:
            probe = subprocess.run(
                ["pgrep", "-P", str(proc.pid)],
                stdin=DEVNULL,
                capture_output=True,
                text=True,
            )
            assert (
                probe.returncode == PGREP_EXIT_NO_MATCH
            ), f"pgrep did not report 'no match' as exit {PGREP_EXIT_NO_MATCH}: {probe}"
            assert probe.stdout.strip() == "", f"pgrep unexpectedly found children: {probe.stdout}"

            # WHEN
            result = find_child_process_id_pgrep(sudo_pid=proc.pid)

            # THEN
            assert result is None
        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.parametrize(
        "returncode,output",
        [
            pytest.param(2, "pgrep: illegal option -- Q\n", id="usage-error"),
            pytest.param(3, "pgrep: cannot open kernel memory\n", id="fatal-error"),
            pytest.param(127, "sh: pgrep: command not found\n", id="not-found"),
        ],
    )
    def test_other_nonzero_exits_still_raise(self, returncode: int, output: str) -> None:
        """The other half of the fix: only exit 1 is benign.

        Treating every non-zero exit as "no child yet" would silently turn a
        broken or missing pgrep into "this sudo has no workload", which is the
        same lost-signal-target outcome by a different route.

        Both halves of the message are pinned. The exit code alone names a
        category of failure without naming the failure: this call merges stderr
        into stdout, so the message is the only place pgrep's own explanation
        survives, and the exception is caught and logged as a warning rather
        than propagated, so there is no traceback to fall back on.
        """
        # GIVEN
        with patch(
            "openjd.sessions._linux._sudo.run", return_value=pgrep_result(returncode, output)
        ):
            # WHEN
            with pytest.raises(FindSignalTargetError) as excinfo:
                find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        message = str(excinfo.value)
        assert f"exited {returncode}" in message
        # repr'd, and so quoted: an operator can see leading/trailing whitespace
        # and can tell an empty explanation from a missing one.
        assert repr(output.strip()) in message

    def test_single_child_is_returned(self) -> None:
        """Exit 0 with one pid: the pid is the answer."""
        # GIVEN
        with patch("openjd.sessions._linux._sudo.run", return_value=pgrep_result(0, "4321\n")):
            # WHEN
            result = find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert result == 4321

    def test_multiple_children_raise(self) -> None:
        """Exit 0 with several pids violates the assumption that sudo has exactly
        one child, so it must not be resolved by guessing one of them."""
        # GIVEN
        with patch(
            "openjd.sessions._linux._sudo.run", return_value=pgrep_result(0, "4321\n4322\n")
        ):
            # WHEN
            with pytest.raises(FindSignalTargetError) as excinfo:
                find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert "4321" in str(excinfo.value) and "4322" in str(excinfo.value)

    def test_no_output_returns_none(self) -> None:
        """Exit 0 with no pids is the same "none yet" answer as exit 1, and must
        stay distinguishable from it only in how it is reached."""
        # GIVEN
        with patch("openjd.sessions._linux._sudo.run", return_value=pgrep_result(0, "")):
            # WHEN
            result = find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert result is None


@pytest.mark.skipif(not is_posix(), reason="pgrep and process groups are posix-only")
class TestFindSudoChildProcessGroupId:
    """Tests for find_sudo_child_process_group_id() over the pgrep lookup."""

    def test_keeps_scanning_until_a_late_child_appears(
        self,
        tmp_path: Path,
        python_exe: str,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """The behaviour the fix exists for: an empty scan must not end the
        retries.

        A parent that gains its child only after a delay reproduces the real
        race -- sudo forks before the kernel finishes creating the workload. When
        an empty pgrep raised FindSignalTargetError, the caller's handler (which
        sits outside its retry loop) swallowed it, logged a warning and returned
        None, so the process group was never recorded and a later cancel had
        nothing to signal. Asserting the *correct* pgid, not merely "not None",
        is what makes this a pin of the fix rather than of the constant.

        is_linux is forced False so that the pgrep path is the one exercised on
        every POSIX host; on Linux the caller would otherwise take the procfs
        path, which already returns None for this situation.
        """
        # GIVEN
        if shutil.which("pgrep") is None:
            pytest.skip("pgrep is not installed on this host")
        parent_script = tmp_path / "parent.py"
        parent_script.write_text(PARENT_SCRIPT)
        child_script = tmp_path / "child.py"
        child_script.write_text(CHILD_SCRIPT)
        pgid_file = tmp_path / "child_pgid.txt"
        child_appears_after_seconds = 0.4
        logger = build_logger(queue_handler)

        parent = Popen(
            [
                python_exe,
                str(parent_script),
                python_exe,
                str(child_script),
                str(pgid_file),
                str(child_appears_after_seconds),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        )
        child_pgid: Optional[int] = None
        try:
            # WHEN
            with patch("openjd.sessions._linux._sudo.is_linux", return_value=False):
                # A window far wider than the delay, so a slow host cannot fail
                # this spuriously. The scan returns as soon as it finds the
                # child, and the defect returns None on the first poll, so the
                # wide window costs nothing either way.
                result = find_sudo_child_process_group_id(
                    logger=logger,
                    sudo_process=parent,
                    timeout_seconds=10.0,
                )

            # THEN
            child_pgid = poll_for_pgid(pgid_file, timeout_seconds=5.0)
            assert child_pgid is not None, (
                "the child never reported its process group, so this test could not have "
                f"observed one (parent exit code: {parent.poll()})"
            )
            assert result == child_pgid
            # AND the scan did not report a failure along the way.
            messages = collect_queue_messages(message_queue)
            assert not any(
                "Unable to determine signal target" in message for message in messages
            ), messages
        finally:
            parent.kill()
            parent.wait()
            if child_pgid is None:
                child_pgid = poll_for_pgid(pgid_file, timeout_seconds=0)
            if child_pgid is not None:
                try:
                    os.killpg(child_pgid, signal.SIGKILL)  # type: ignore
                except (ProcessLookupError, PermissionError):
                    # Best-effort teardown. The group is already gone
                    # (ProcessLookupError) or was never ours to signal
                    # (PermissionError); either way there is nothing left to clean
                    # up and failing here would mask the test's real result.
                    pass


@pytest.mark.skipif(not is_posix(), reason="pgrep and process groups are posix-only")
class TestFindSudoChildProcessGroupIdScanErrors:
    """Tests that a single bad scan does not end the retries.

    Defect pinned: the `except FindSignalTargetError` that guards the scan used to
    sit OUTSIDE the retry `while`, so the *first* bad scan aborted the whole
    search -- the process group was never recorded, and a later cancel had
    nothing to signal. Every condition the loop races is transient by
    construction: the procfs scan sees more than one child while sudo's fork is
    still settling, and a `pgrep` invocation can fail for reasons that do not
    recur on the next poll.

    The scan is mocked here, unlike the end-to-end test above, because a *failing*
    scan is not something a real host can be asked for on demand -- and both
    branches (procfs and pgrep) have to be covered from one platform.
    """

    @pytest.mark.parametrize(
        "on_linux,scan_function",
        [
            pytest.param(True, "find_sudo_child_process_id_procfs", id="procfs"),
            pytest.param(False, "find_child_process_id_pgrep", id="pgrep"),
        ],
    )
    def test_transient_scan_error_does_not_end_the_retries(
        self,
        on_linux: bool,
        scan_function: str,
        sleeper: Callable[..., Popen],
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """One raising scan followed by a succeeding one still yields the pgid.

        Parametrized over both lookups because the two are selected by is_linux()
        and the defect was in the shared caller, so a fix verified on one branch
        says nothing about the other.
        """
        # GIVEN
        sudo_process = sleeper()
        # The workload, in its own process group as sudo's child would be.
        workload = sleeper(own_process_group=True)
        logger = build_logger(queue_handler)
        scan = Mock(side_effect=[FindSignalTargetError("transient"), workload.pid])

        # WHEN
        with (
            patch(f"openjd.sessions._linux._sudo.{scan_function}", scan),
            patch("openjd.sessions._linux._sudo.is_linux", return_value=on_linux),
        ):
            result = find_sudo_child_process_group_id(
                logger=logger,
                sudo_process=sudo_process,
                timeout_seconds=10.0,
            )

        # THEN
        # start_new_session made the workload a process-group leader, so its pgid
        # is its own pid. Asserting against that, rather than re-running the
        # production expression os.getpgid(pid), keeps the expected value
        # independent of the code under test.
        assert result == workload.pid
        assert scan.call_count == 2, "the scan was not retried after it raised"
        # AND nothing was logged at all. The two debug lines on this path sit below
        # the level build_logger sets, so an empty queue says more than the absence
        # of one particular string -- which a reworded warning would satisfy
        # silently.
        assert collect_queue_messages(message_queue) == []

    def test_scan_that_always_fails_times_out_and_reports_the_last_error(
        self,
        sleeper: Callable[..., Popen],
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """Retrying is not the same as retrying forever.

        A scan that never succeeds must still end at the timeout, with None and
        the warning -- and the warning must carry the last scan error, because
        with the error no longer aborting the search it is otherwise discarded
        and the timeout message alone cannot say what kept going wrong.
        """
        # GIVEN
        sudo_process = sleeper()
        logger = build_logger(queue_handler)
        # The wording find_child_process_id_pgrep really uses for this, so that a
        # reader is not left wondering whether it was invented.
        scan_error = "Expected a single child process of sudo, but found ['4321', '4322']"
        scan = Mock(side_effect=FindSignalTargetError(scan_error))

        # WHEN
        with (
            patch("openjd.sessions._linux._sudo.find_child_process_id_pgrep", scan),
            patch("openjd.sessions._linux._sudo.is_linux", return_value=False),
        ):
            result = find_sudo_child_process_group_id(
                logger=logger,
                sudo_process=sudo_process,
                # A whole second rather than a fraction: the assertion below needs a
                # second iteration to have begun and each one sleeps 0.05s, so a
                # tight budget would turn a single overrunning sleep on a loaded
                # host into a failure. The scan never succeeds, so the full window
                # is spent either way.
                timeout_seconds=1.0,
            )

        # THEN
        assert result is None
        assert scan.call_count > 1, "the scan was not retried after it raised"
        messages = collect_queue_messages(message_queue)
        warnings = [m for m in messages if "Unable to determine signal target" in m]
        assert len(warnings) == 1, messages
        # The timeout is what is reported, not the individual scan failure: that
        # distinction is the whole difference between the fixed and broken code.
        assert "unable to detect subprocess before timeout" in warnings[0]
        assert f"last scan error: {scan_error}" in warnings[0]


@pytest.mark.skipif(not is_posix(), reason="pgrep and process groups are posix-only")
class TestFindSudoChildProcessGroupIdProcessGroupRaces:
    """Tests of how find_sudo_child_process_group_id() resolves the two races that
    are not scan failures: the child exiting before its group can be read, and the
    child not having left sudo's process group yet.

    Both behaviours predate the retry-scoping fix above and had to survive it, so
    they are pinned separately from it. The scan is mocked for the same reason as
    above -- these are races, and a test that waited for one to occur naturally
    would be a test that usually skipped the interesting path.
    """

    def test_child_that_exits_mid_scan_still_short_circuits(
        self,
        sleeper: Callable[..., Popen],
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """A child that is gone by the time its group is read is answered
        immediately with None.

        Retrying that would burn the whole timeout window on a process that is
        known to be gone, and would report it as a timeout rather than as the
        ordinary "it already exited" it is -- which is why no warning is logged.
        """
        # GIVEN a pid that has been reaped, so os.getpgid() raises for it
        departed = sleeper(own_process_group=True)
        departed.kill()
        departed.wait()
        with pytest.raises(ProcessLookupError):
            os.getpgid(departed.pid)  # type: ignore
        sudo_process = sleeper()
        logger = build_logger(queue_handler)
        scan = Mock(return_value=departed.pid)

        # WHEN
        with (
            patch("openjd.sessions._linux._sudo.find_child_process_id_pgrep", scan),
            patch("openjd.sessions._linux._sudo.is_linux", return_value=False),
        ):
            result = find_sudo_child_process_group_id(
                logger=logger,
                sudo_process=sudo_process,
                timeout_seconds=5.0,
            )

        # THEN
        assert result is None
        assert scan.call_count == 1, "an exited child was scanned for again"
        # AND nothing was logged: this is an ordinary outcome, not a failure to
        # report. Asserted as an empty queue rather than as the absence of one
        # string, which a reworded warning would satisfy silently.
        assert collect_queue_messages(message_queue) == []

    def test_child_still_sharing_sudos_process_group_is_never_accepted(
        self,
        sleeper: Callable[..., Popen],
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """sudo's own process group is not an answer.

        Accepting it would aim a later SIGKILL at the group containing this
        process, so a cancel would take down the session runtime along with the
        workload. Here the group never changes, so the loop must exhaust the
        timeout rather than accept what it has.
        """
        # GIVEN a live process in the same process group as the stand-in for sudo
        sudo_process = sleeper()
        sibling = sleeper()
        assert os.getpgid(sibling.pid) == os.getpgid(  # type: ignore
            sudo_process.pid
        ), "test setup: the two processes were expected to share a process group"
        logger = build_logger(queue_handler)
        scan = Mock(return_value=sibling.pid)

        # WHEN
        with (
            patch("openjd.sessions._linux._sudo.find_child_process_id_pgrep", scan),
            patch("openjd.sessions._linux._sudo.is_linux", return_value=False),
        ):
            result = find_sudo_child_process_group_id(
                logger=logger,
                sudo_process=sudo_process,
                timeout_seconds=0.2,
            )

        # THEN
        assert result is None
        messages = collect_queue_messages(message_queue)
        warnings = [m for m in messages if "Unable to determine signal target" in m]
        assert len(warnings) == 1, messages
        assert "unable to detect subprocess before timeout" in warnings[0]
        # AND no scan error is invented: nothing here failed to scan.
        assert "last scan error" not in warnings[0]

    def test_child_is_accepted_once_it_leaves_sudos_process_group(
        self,
        tmp_path: Path,
        sleeper: Callable[..., Popen],
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        """The other side of the same behaviour: sharing sudo's group is a reason
        to retry, not to give up.

        The child moves into its own group partway through the retry window, as
        sudo's child really does, and the pgid it reports for itself is what the
        function must return.
        """
        # GIVEN
        pgid_file = tmp_path / "child_pgid.txt"
        sudo_process = sleeper()
        logger = build_logger(queue_handler)
        # Started without own_process_group, so it begins life in this process'
        # group -- the same group as the stand-in for sudo -- and calls setsid()
        # only after the delay. Registered with the fixture, so it is reaped even
        # if the setup assertion below fails.
        child = sleeper(
            script=LATE_PROCESS_GROUP_SCRIPT
            % {"pgid_file": repr(str(pgid_file)), "delay_seconds": 0.5}
        )
        scan = Mock(return_value=child.pid)

        # The delay gives this assertion ample room: the child has to start a
        # Python interpreter and sleep 0.5s before it can change groups.
        assert os.getpgid(child.pid) == os.getpgid(  # type: ignore
            sudo_process.pid
        ), "test setup: the child was expected to start in sudo's process group"

        # WHEN
        with (
            patch("openjd.sessions._linux._sudo.find_child_process_id_pgrep", scan),
            patch("openjd.sessions._linux._sudo.is_linux", return_value=False),
        ):
            result = find_sudo_child_process_group_id(
                logger=logger,
                sudo_process=sudo_process,
                timeout_seconds=10.0,
            )

        # THEN
        reported_pgid = poll_for_pgid(pgid_file, timeout_seconds=5.0)
        assert reported_pgid is not None, (
            "the child never reported a new process group, so this test could not have "
            f"observed one (exit code: {child.poll()}, stderr: {stderr_of(child)!r})"
        )
        assert result == reported_pgid
        assert result != os.getpgid(sudo_process.pid)  # type: ignore
        # AND nothing was logged: retrying past the shared group is not a failure.
        assert collect_queue_messages(message_queue) == []
