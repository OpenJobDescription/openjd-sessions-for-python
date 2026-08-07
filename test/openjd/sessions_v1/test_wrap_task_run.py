# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests that the _v1 Session.run_task accepts and forwards step_name
to the Rust binding without error (RFC 0008).

The full wrap-action integration (``{{WrappedStep.Name}}`` resolution inside an
``onWrapTaskRun`` hook) is validated by the worker-agent's differential runtime
test. This test verifies the _v1 Python wrapper plumbs the kwarg through.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from openjd.model._v1 import create_job, decode_job_template
from openjd.sessions._v1 import Session, SessionState, ActionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_TEMPLATE = {
    "specificationVersion": "jobtemplate-2023-09",
    "name": "StepNameTest",
    "steps": [
        {
            "name": "MyStep",
            "script": {
                "actions": {
                    "onRun": {"command": "echo", "args": ["hello-from-task"]},
                }
            },
        }
    ],
}


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestV1RunTaskStepName:
    """Verify that _v1 Session.run_task forwards step_name to the Rust binding."""

    def test_run_task_accepts_step_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """run_task(step_name=...) succeeds and the task completes normally."""
        job_template = decode_job_template(template=_SIMPLE_TEMPLATE)
        job = create_job(job_template=job_template, job_parameter_values={})
        step = job.steps[0]

        with Session(
            session_id=uuid.uuid4().hex,
            job_parameter_values={},
            session_root_directory=tmp_path,
        ) as session:
            session.run_task(
                step_script=step.script,
                task_parameter_values={},
                resolved_symtab=step.resolved_symtab,
                step_name="MyStep",
            )
            _run_until_ready(session)

            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert session.action_status.exit_code == 0

        messages = "\n".join(caplog.messages)
        assert "hello-from-task" in messages

    def test_run_task_step_name_none_also_works(self, tmp_path: Path) -> None:
        """step_name=None (the default) still works."""
        job_template = decode_job_template(template=_SIMPLE_TEMPLATE)
        job = create_job(job_template=job_template, job_parameter_values={})
        step = job.steps[0]

        with Session(
            session_id=uuid.uuid4().hex,
            job_parameter_values={},
            session_root_directory=tmp_path,
        ) as session:
            session.run_task(
                step_script=step.script,
                task_parameter_values={},
                resolved_symtab=step.resolved_symtab,
                # step_name defaults to None
            )
            _run_until_ready(session)

            assert session.state == SessionState.READY
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert session.action_status.exit_code == 0
