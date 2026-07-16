# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions._linux._sudo import (
    FindSignalTargetError,
    find_child_process_id_pgrep,
)


def _pgrep_result(returncode: int, stdout: str = "") -> CompletedProcess:
    return CompletedProcess(args=["pgrep"], returncode=returncode, stdout=stdout)


class TestFindChildProcessIdPgrep:
    """Tests for the pgrep-based signal-target discovery used on non-Linux POSIX hosts."""

    @patch("openjd.sessions._linux._sudo.run")
    def test_returns_child_pid_on_match(self, mock_run: MagicMock) -> None:
        # GIVEN pgrep matches a single child process
        mock_run.return_value = _pgrep_result(returncode=0, stdout="4321\n")

        # WHEN
        result = find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert result == 4321

    @patch("openjd.sessions._linux._sudo.run")
    def test_returns_none_when_no_match_yet(self, mock_run: MagicMock) -> None:
        # GIVEN pgrep finds no matching processes (exit code 1) -- e.g. sudo has not
        # spawned its child yet. This must return None (so the caller retries), NOT raise.
        mock_run.return_value = _pgrep_result(returncode=1, stdout="")

        # WHEN
        result = find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert result is None

    @patch("openjd.sessions._linux._sudo.run")
    def test_raises_on_pgrep_error(self, mock_run: MagicMock) -> None:
        # GIVEN pgrep reports an actual error (exit code > 1)
        mock_run.return_value = _pgrep_result(returncode=2, stdout="")

        # WHEN / THEN
        with pytest.raises(FindSignalTargetError):
            find_child_process_id_pgrep(sudo_pid=1234)

    @patch("openjd.sessions._linux._sudo.run")
    def test_raises_on_multiple_children(self, mock_run: MagicMock) -> None:
        # GIVEN pgrep matches more than one child, violating the single-child assumption
        mock_run.return_value = _pgrep_result(returncode=0, stdout="4321\n4322\n")

        # WHEN / THEN
        with pytest.raises(FindSignalTargetError):
            find_child_process_id_pgrep(sudo_pid=1234)

    @patch("openjd.sessions._linux._sudo.run")
    def test_returns_none_on_empty_stdout_with_success(self, mock_run: MagicMock) -> None:
        # GIVEN pgrep exits 0 but with no pids in stdout (defensive: treat as no match)
        mock_run.return_value = _pgrep_result(returncode=0, stdout="")

        # WHEN
        result = find_child_process_id_pgrep(sudo_pid=1234)

        # THEN
        assert result is None
