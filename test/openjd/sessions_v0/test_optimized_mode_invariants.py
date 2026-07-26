# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Runtime invariants must survive ``python -O``, which strips every ``assert``.

Checks that carry a real invariant -- as opposed to type-checker narrowing --
have to be explicit raises. See the policy in DEVELOPMENT.md.
"""

import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import (
    DataString as DataString_2023_09,
    EmbeddedFileText as EmbeddedFileText_2023_09,
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)

from openjd.sessions import Session
from openjd.sessions._embedded_files import (
    EmbeddedFiles,
    EmbeddedFilesScope,
    _FileRecord,
)
from openjd.sessions._runner_base import ScriptRunnerBase
from openjd.sessions._subprocess import LoggingSubprocess


class _Runner(ScriptRunnerBase):
    """Minimal concrete runner: only _generate_command_shell_script is exercised."""

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        pass


# ===========================================================================
# R5-1 -- redaction must not leave record.args populated
# ===========================================================================


class TestInvariantsSurviveOptimizedMode:
    """R5-6: `assert` is stripped under `python -O`, which is a legitimate
    production deployment mode. Checks that carry a real runtime invariant must
    be explicit raises."""

    def test_unsupported_embedded_file_model_raises(self, tmp_path: Path) -> None:
        # GIVEN: an embedded file object of an unrecognised shape
        files = EmbeddedFiles(
            logger=MagicMock(),
            scope=EmbeddedFilesScope.ENV,
            session_files_directory=tmp_path,
        )

        class _AlienFile:
            filename = "x.sh"
            runnable = False

        # WHEN / THEN: each of the three entry points stops rather than
        # materializing a file from an object it does not understand.
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._find_value_prefix(_AlienFile())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._get_symtab_entry(_AlienFile())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._materialize_file(
                tmp_path / "x.sh", _AlienFile(), SymbolTable()  # type: ignore[arg-type]
            )

    def test_working_directory_property_raises_a_legible_error(self) -> None:
        # GIVEN: a Session whose construction did not complete
        session = Session.__new__(Session)
        session._working_dir = None  # type: ignore[attr-defined]

        # WHEN / THEN: a library RuntimeError, not AttributeError on None.
        with pytest.raises(RuntimeError, match="no working directory"):
            _ = session.working_directory

    def test_state_is_readable_when_a_launch_failed_after_process_creation(
        self, tmp_path: Path
    ) -> None:
        """The `state` property is on the path every consumer polls. An
        inconsistent (process set, future unset) pair must not make the runner
        permanently unreadable."""
        # GIVEN
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            runner._process = MagicMock()
            runner._run_future = None

            # WHEN / THEN: no AssertionError, no AttributeError.
            from openjd.sessions._runner_base import ScriptRunnerState

            assert runner.state == ScriptRunnerState.READY
        finally:
            runner.shutdown()

    def test_notify_period_end_is_a_no_op_without_a_process(self, tmp_path: Path) -> None:
        """Runs on a threading.Timer thread, where an exception reaches only
        threading.excepthook -- so the grace period would appear to have silently
        done nothing."""
        # GIVEN
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            runner._process = None

            # WHEN / THEN: returns cleanly.
            runner._on_notify_period_end()
        finally:
            runner.shutdown()

    def test_log_subproc_stdout_raises_a_legible_error_without_a_process(
        self, tmp_path: Path
    ) -> None:
        # GIVEN
        proc = LoggingSubprocess(logger=MagicMock(), args=[sys.executable, "-c", "pass"])

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="before the subprocess has been created"):
            proc._log_subproc_stdout()

    def test_an_embedded_file_still_materializes_normally(self, tmp_path: Path) -> None:
        """Guard against the isinstance rewrite breaking the happy path."""
        # GIVEN
        files = EmbeddedFiles(
            logger=MagicMock(),
            scope=EmbeddedFilesScope.ENV,
            session_files_directory=tmp_path,
        )
        model = EmbeddedFileText_2023_09(
            name="Script",
            type=EmbeddedFileTypes_2023_09.TEXT,
            data=DataString_2023_09("hello"),
        )
        symtab = SymbolTable()

        # WHEN
        records = files.allocate_file_paths([model], symtab)
        files.write_file_contents(records, symtab)

        # THEN
        assert len(records) == 1
        assert isinstance(records[0], _FileRecord)
        assert records[0].filename.read_text() == "hello"


# ===========================================================================
# R5-9 -- an unknown process group must be recorded as unknown
# ===========================================================================
