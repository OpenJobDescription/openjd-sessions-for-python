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


from openjd.sessions._os_checker import is_posix
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
