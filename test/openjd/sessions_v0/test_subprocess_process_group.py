# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Recording the process group of a launched subprocess.

The recorded group is the only signal target a later cancel has, so an unknown
group has to be recorded as unknown rather than guessed.
"""

import sys
from logging.handlers import QueueHandler
from queue import SimpleQueue
from unittest.mock import MagicMock, patch

import pytest


from openjd.sessions import _subprocess as subprocess_mod
from openjd.sessions._os_checker import is_posix
from openjd.sessions._session_user import PosixSessionUser
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger


@pytest.mark.skipif(not is_posix(), reason="process groups are POSIX-only")
class TestProcessGroupIsUnknownWhenItCannotBeLookedUp:
    """R5-9: the lookup fails *because* the process is gone, so its pid is dead.
    Recording it as a process-group id means a later `killpg` is at best a no-op
    and, after pid recycling, targets an unrelated group."""

    def test_reaped_child_leaves_the_group_unknown(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN: a child whose process group cannot be looked up
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(
            logger=logger, args=[sys.executable, "-c", "pass"], os_env_vars=None
        )

        # WHEN
        with patch(
            "openjd.sessions._subprocess.os.getpgid", side_effect=ProcessLookupError(3, "No such")
        ):
            proc.run()

        # THEN: "unknown", not a stale pid -- and the action still succeeded,
        # which is the behaviour the original fix existed to protect.
        assert proc._sudo_child_process_group_id is None
        assert proc.exit_code == 0
        assert proc.failed_to_start is False

    def test_no_signal_is_delivered_when_the_group_is_unknown(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN: a finished process with no known group
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[sys.executable, "-c", "pass"])
        with patch(
            "openjd.sessions._subprocess.os.getpgid", side_effect=ProcessLookupError(3, "No such")
        ):
            proc.run()
        assert proc._sudo_child_process_group_id is None

        # WHEN: a SIGKILL is attempted anyway
        with (
            patch("openjd.sessions._subprocess.os.killpg") as killpg,
            patch(
                "openjd.sessions._subprocess.find_sudo_child_process_group_id", return_value=None
            ),
        ):
            proc._posix_signal_subprocess(MagicMock(pid=999999), signal_name="kill")

        # THEN: nothing was signalled.
        killpg.assert_not_called()

    def test_sudo_helper_returns_unknown_when_sudo_is_already_gone(self) -> None:
        """Sibling: the same guard on the first getpgid in the sudo helper."""
        # GIVEN
        from openjd.sessions._linux._sudo import find_sudo_child_process_group_id

        # WHEN
        with patch(
            "openjd.sessions._linux._sudo.os.getpgid",
            side_effect=ProcessLookupError(3, "No such process"),
        ):
            result = find_sudo_child_process_group_id(
                logger=MagicMock(), sudo_process=MagicMock(pid=999999)
            )

        # THEN: the established "unknown" value, not an escaping ESRCH.
        assert result is None


@pytest.mark.skipif(not is_posix(), reason="posix signal delivery")
class TestSignalArgvCommandResolution:
    """How `kill` is spelled in the signal argv, per branch.

    The two branches deliberately differ, and the difference is easy to "tidy"
    into a bug in either direction, so both are pinned here.

    Cross-user goes through `sudo -u <user> -i kill ...`. `sudo -i` simulates an
    initial login: it starts the target user's login shell and passes the command
    to it via -c, so `kill` is a **shell builtin** in that position. Nothing is
    resolved on PATH and -i has already reset the environment, so a job-controlled
    PATH cannot reach it. Resolving it to an absolute path would instead invent a hard
    dependency on kill(1) from procps, which Debian `-slim` images do not install,
    and would break every cross-user SIGKILL fallback on such a host.

    Same-user goes through `run([...])`, which really does execvp() a bare name
    against this process's PATH, so there the resolution is load-bearing.
    """

    def _signal_and_capture_argv(self, user, message_queue, queue_handler) -> list[str]:
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=["/path/to/workload.sh"], user=user)
        proc._sudo_child_process_group_id = 4321

        with (
            patch.object(subprocess_mod, "is_posix", return_value=True),
            patch.object(subprocess_mod, "is_windows", return_value=False),
            patch.object(subprocess_mod, "is_linux", return_value=False),
            patch.object(
                subprocess_mod,
                "system_command_path",
                side_effect=lambda name: f"/trusted/{name}",
            ),
            patch.object(subprocess_mod, "os") as mock_os,
            patch.object(subprocess_mod, "run") as mock_run,
        ):
            mock_os.killpg.side_effect = OSError(1, "not permitted")
            mock_run.return_value = MagicMock(returncode=0)
            proc._posix_signal_subprocess(MagicMock(pid=999999), signal_name="kill")

        assert mock_run.call_count == 1, "the sudo/run fallback did not execute"
        return list(mock_run.call_args.args[0])

    def test_cross_user_leaves_kill_bare_for_the_login_shell_builtin(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN
        user = MagicMock(spec=PosixSessionUser)
        user.user = "job-user"
        user.is_process_user.return_value = False

        # WHEN
        argv = self._signal_and_capture_argv(user, message_queue, queue_handler)

        # THEN
        assert argv == [
            "/trusted/sudo",
            "-u",
            "job-user",
            "-i",
            "kill",
            "-s",
            "kill",
            "--",
            "-4321",
        ]
        # Spelled out separately from the equality above, because this is the
        # property that matters and the reason for it is not obvious from the list.
        assert (
            "kill" in argv and "/trusted/kill" not in argv
        ), "kill must stay a bare name so the login shell builtin is used"

    def test_same_user_resolves_kill_because_run_execvps_it(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN
        user = MagicMock(spec=PosixSessionUser)
        user.user = "same-user"
        user.is_process_user.return_value = True

        # WHEN
        argv = self._signal_and_capture_argv(user, message_queue, queue_handler)

        # THEN
        assert argv == ["/trusted/kill", "-s", "kill", "--", "-4321"]
        assert "sudo" not in " ".join(argv)
