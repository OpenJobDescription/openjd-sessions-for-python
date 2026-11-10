# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""End-to-end test of the Rust session runtime's structured log fields.

Runs a real `openjd.sessions._v1.Session` task, captures the records that land
on the "openjd.sessions" logger, and checks that the fields the Rust session
runtime attaches to them arrive with the Python types that the logger
documents: a `LogContent` for `openjd_log_content`, the session's id as a `str`
for `session_id`, and an `int` for `openjd_timestamp_usec`.

The fields only cross the Rust/Python boundary at all when the installed
openjd-model build enables pyo3-log's structured-logging support. Builds that
do not drop every structured field silently, so the tests here are gated on a
feature probe -- running one real session and looking for `session_id` on any
record. They skip on a build without the support and start passing on the
first build that has it, without a code change here.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

import pytest

from openjd.model._v1 import create_job, decode_job_template
from openjd.sessions import LogContent
from openjd.sessions._v1 import Session, SessionState

LOGGER_NAME = "openjd.sessions"

_TASK_OUTPUT = "openjd-rust-log-tag-probe"

# `sys.executable -c ...` rather than a shell builtin such as `echo`, so the
# same command works on Linux, macOS and Windows.
_TEMPLATE = {
    "specificationVersion": "jobtemplate-2023-09",
    "name": "RustLogTags",
    "steps": [
        {
            "name": "Emit",
            "script": {
                "actions": {
                    "onRun": {
                        "command": sys.executable,
                        "args": ["-c", f"print({_TASK_OUTPUT!r})"],
                    }
                }
            },
        }
    ],
}

# Generous enough for a cold start of a real subprocess on a loaded CI worker,
# and well inside the suite's per-test timeout.
_ACTION_TIMEOUT_S = 15.0
_QUIESCENT_STATES = (SessionState.READY, SessionState.ENDED, SessionState.READY_ENDING)


def records_carry_rust_tags(records: Iterable[logging.LogRecord]) -> bool:
    """Whether `records` show that the Rust runtime's structured fields arrived.

    The runtime tags the records it emits with `session_id`, and that field
    survives the trip into Python unchanged when the openjd-model build
    forwards structured fields at all. Its presence on any record is therefore
    the feature probe, and it is the one field that needs no normalization, so
    the probe does not depend on the code under test.
    """
    return any(hasattr(record, "session_id") for record in records)


class _CollectingHandler(logging.Handler):
    """Keeps every record it is given, for inspection by a test."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _SessionRun(NamedTuple):
    session_id: str
    records: list[logging.LogRecord]


def _run_one_task(session_root: Path) -> _SessionRun:
    """Run one real task in a Rust-backed Session, returning what it logged."""
    job_template = decode_job_template(template=_TEMPLATE)
    job = create_job(job_template=job_template, job_parameter_values={})
    step = job.steps[0]

    session_id = f"rust-log-tags-{uuid.uuid4().hex}"
    handler = _CollectingHandler()
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    try:
        with Session(
            session_id=session_id,
            job_parameter_values={},
            session_root_directory=session_root,
        ) as session:
            session.run_task(
                step_script=step.script,
                task_parameter_values={},
                resolved_symtab=step.resolved_symtab,
            )
            deadline = time.monotonic() + _ACTION_TIMEOUT_S
            while session.state not in _QUIESCENT_STATES and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (
                session.state in _QUIESCENT_STATES
            ), f"Session did not settle within {_ACTION_TIMEOUT_S}s; state={session.state}"
    finally:
        logger.removeHandler(handler)

    return _SessionRun(session_id=session_id, records=handler.records)


class TestFeatureProbe:
    """The probe that gates the end-to-end tests below, tested on its own so the
    gate cannot quietly stop working."""

    def test_no_records_means_no_tags(self) -> None:
        assert records_carry_rust_tags([]) is False

    def test_untagged_records_mean_no_tags(self) -> None:
        records = [logging.makeLogRecord({"msg": "untagged"}) for _ in range(3)]
        assert records_carry_rust_tags(records) is False

    def test_one_tagged_record_is_enough(self) -> None:
        records = [
            logging.makeLogRecord({"msg": "untagged"}),
            logging.makeLogRecord({"msg": "tagged", "session_id": "s"}),
        ]
        assert records_carry_rust_tags(records) is True


@pytest.fixture(scope="module")
def session_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_SessionRun]:
    """One real session run, shared by the tests that inspect its records."""
    yield _run_one_task(tmp_path_factory.mktemp("rust-log-tags"))


@pytest.fixture
def tagged_session_run(session_run: _SessionRun) -> _SessionRun:
    """`session_run`, or a skip when this build does not forward the tags."""
    if not records_carry_rust_tags(session_run.records):
        pytest.skip(
            "The installed openjd-model build does not forward the Rust session "
            "runtime's structured log fields to Python, so there is nothing to "
            "normalize. This test runs against a build that does."
        )
    return session_run


@pytest.mark.xdist_group("rust_log_tags")
class TestRustSessionLogTags:
    """A real Rust-backed session's records reach consumers with the documented
    Python types."""

    def test_the_session_logged_something(self, session_run: _SessionRun) -> None:
        """Ungated: a run that logged nothing would make every assertion below
        vacuous, including the feature probe's."""
        assert session_run.records
        assert any(_TASK_OUTPUT in record.getMessage() for record in session_run.records)

    def test_log_content_is_the_public_log_content_class(
        self, tagged_session_run: _SessionRun
    ) -> None:
        # GIVEN
        tagged = [
            record for record in tagged_session_run.records if hasattr(record, "openjd_log_content")
        ]

        # THEN
        assert tagged, "No record carried openjd_log_content"
        for record in tagged:
            assert isinstance(record.openjd_log_content, LogContent), (  # type: ignore[attr-defined]
                f"openjd_log_content was {record.openjd_log_content!r} "  # type: ignore[attr-defined]
                f"on message {record.getMessage()!r}"
            )

    def test_session_id_identifies_this_session(self, tagged_session_run: _SessionRun) -> None:
        # GIVEN
        tagged = [record for record in tagged_session_run.records if hasattr(record, "session_id")]

        # THEN
        assert tagged, "No record carried session_id"
        for record in tagged:
            assert record.session_id == tagged_session_run.session_id  # type: ignore[attr-defined]

    def test_timestamp_is_an_int(self, tagged_session_run: _SessionRun) -> None:
        # GIVEN
        tagged = [
            record
            for record in tagged_session_run.records
            if hasattr(record, "openjd_timestamp_usec")
        ]

        # THEN
        assert tagged, "No record carried openjd_timestamp_usec"
        for record in tagged:
            assert type(record.openjd_timestamp_usec) is int  # type: ignore[attr-defined]
            assert record.openjd_timestamp_usec > 0  # type: ignore[attr-defined]

    def test_command_output_is_tagged_as_command_output(
        self, tagged_session_run: _SessionRun
    ) -> None:
        """The task's stdout is what a consumer routes to the session log, so
        check the bit the runtime sets on it specifically."""
        # GIVEN
        output_records = [
            record for record in tagged_session_run.records if _TASK_OUTPUT in record.getMessage()
        ]

        # THEN
        assert output_records, "The task's output line was not captured"
        assert any(
            isinstance(getattr(record, "openjd_log_content", None), LogContent)
            and LogContent.COMMAND_OUTPUT in record.openjd_log_content  # type: ignore[attr-defined]
            for record in output_records
        )
