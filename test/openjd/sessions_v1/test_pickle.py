# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pickle round-trip tests for the ``openjd.sessions._v1`` value types.

The Group A enums (``SessionState``, ``ActionState``, ``ScriptRunnerState``)
and Group B value types (``ActionStatus``, ``ActionResult``,
``PosixSessionUser``) all support pickle. ``Session`` itself does not —
it owns live OS resources (subprocess, working directory, file handles)
and explicitly rejects pickling.

Cross-reference: ``reports/sessions-bindings-quality-evaluation-report.md``
recommendations #7 and #9.
"""

import os
import pickle
import sys

import pytest


# ── Group A: enums ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "openjd.sessions._v1.SessionState.READY",
        "openjd.sessions._v1.SessionState.RUNNING",
        "openjd.sessions._v1.SessionState.CANCELING",
        "openjd.sessions._v1.SessionState.READY_ENDING",
        "openjd.sessions._v1.SessionState.ENDED",
        "openjd.sessions._v1.ActionState.RUNNING",
        "openjd.sessions._v1.ActionState.SUCCESS",
        "openjd.sessions._v1.ActionState.FAILED",
        "openjd.sessions._v1.ActionState.CANCELED",
        "openjd.sessions._v1.ActionState.TIMEOUT",
        "openjd.sessions._v1.ScriptRunnerState.READY",
        "openjd.sessions._v1.ScriptRunnerState.RUNNING",
        "openjd.sessions._v1.ScriptRunnerState.CANCELING",
        "openjd.sessions._v1.ScriptRunnerState.CANCELED",
        "openjd.sessions._v1.ScriptRunnerState.TIMEOUT",
        "openjd.sessions._v1.ScriptRunnerState.FAILED",
        "openjd.sessions._v1.ScriptRunnerState.SUCCESS",
    ],
)
def test_enum_round_trip(value):
    import importlib

    module_path, attr = value.rsplit(".", 1)
    cls_path, cls = module_path.rsplit(".", 1)
    module = importlib.import_module(cls_path)
    enum_cls = getattr(module, cls)
    v = getattr(enum_cls, attr)
    loaded = pickle.loads(pickle.dumps(v))
    assert loaded == v
    assert loaded is v


# ── Group B: ActionStatus ────────────────────────────────────────


def test_action_status_round_trip_minimal():
    from openjd.sessions._v1 import ActionState, ActionStatus

    status = ActionStatus(state=ActionState.RUNNING)
    loaded = pickle.loads(pickle.dumps(status))
    assert loaded.state == status.state
    assert loaded.progress is None
    assert loaded.status_message is None
    assert loaded.fail_message is None
    assert loaded.exit_code is None
    assert loaded.started_at is None
    assert loaded.ended_at is None


def test_action_status_round_trip_full_user_constructed():
    from openjd.sessions._v1 import ActionState, ActionStatus

    status = ActionStatus(
        state=ActionState.SUCCESS,
        progress=100.0,
        status_message="ok",
        fail_message=None,
        exit_code=0,
    )
    loaded = pickle.loads(pickle.dumps(status))
    assert loaded.state == status.state
    assert loaded.progress == 100.0
    assert loaded.status_message == "ok"
    assert loaded.fail_message is None
    assert loaded.exit_code == 0


def test_action_status_round_trip_failed():
    from openjd.sessions._v1 import ActionState, ActionStatus

    status = ActionStatus(
        state=ActionState.FAILED,
        fail_message="non-zero exit",
        exit_code=1,
    )
    loaded = pickle.loads(pickle.dumps(status))
    assert loaded.state == ActionState.FAILED
    assert loaded.fail_message == "non-zero exit"
    assert loaded.exit_code == 1


def test_action_status_pickle_with_timestamps():
    """``started_at`` / ``ended_at`` are normally set internally by the
    runner; the pickle reducer goes through ``_from_state`` which
    accepts them. Round-trip via the private classmethod."""
    import datetime

    from openjd.sessions._v1 import ActionState, ActionStatus

    started = datetime.datetime(2024, 1, 15, 12, 30, 0, tzinfo=datetime.timezone.utc)
    ended = datetime.datetime(2024, 1, 15, 12, 35, 42, tzinfo=datetime.timezone.utc)
    status = ActionStatus._from_state(
        state=ActionState.SUCCESS,
        exit_code=0,
        started_at=started,
        ended_at=ended,
    )
    loaded = pickle.loads(pickle.dumps(status))
    assert loaded.state == ActionState.SUCCESS
    assert loaded.started_at == started
    assert loaded.ended_at == ended


# ── Group B: ActionResult ────────────────────────────────────────


def test_action_result_round_trip():
    from openjd.sessions._v1 import ActionResult, ActionState

    result = ActionResult(state=ActionState.SUCCESS, exit_code=0, stdout="hello\n")
    loaded = pickle.loads(pickle.dumps(result))
    assert loaded.state == result.state
    assert loaded.exit_code == result.exit_code
    assert loaded.stdout == result.stdout


def test_action_result_round_trip_minimal():
    """Default ``stdout=""`` and ``exit_code=None`` round-trip."""
    from openjd.sessions._v1 import ActionResult, ActionState

    result = ActionResult(state=ActionState.CANCELED)
    loaded = pickle.loads(pickle.dumps(result))
    assert loaded.state == result.state
    assert loaded.exit_code is None
    assert loaded.stdout == ""


# ── Group B: PosixSessionUser ────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_posix_session_user_round_trip():
    from openjd.sessions._v1 import PosixSessionUser

    user_name = os.environ.get("USER", "nobody")
    user = PosixSessionUser(user_name)
    loaded = pickle.loads(pickle.dumps(user))
    assert loaded.user == user.user
    assert loaded.group == user.group


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_posix_session_user_round_trip_with_group():
    from openjd.sessions._v1 import PosixSessionUser

    # Use the current user/group; PosixSessionUser doesn't validate
    # they exist on the system in its constructor.
    user_name = os.environ.get("USER", "nobody")
    user = PosixSessionUser(user_name, group=user_name)
    loaded = pickle.loads(pickle.dumps(user))
    assert loaded.user == user_name
    assert loaded.group == user_name


# ── Pickled-bytes shape sanity ──────────────────────────────────


def test_pickled_action_state_qualifies_under_canonical_module():
    from openjd.sessions._v1 import ActionState

    data = pickle.dumps(ActionState.SUCCESS)
    # The pickled object must reference the canonical module so that
    # consumers reading pickled bytes (Deadline Cloud worker-agent IPC
    # in particular) resolve it correctly.
    assert b"openjd.sessions._v1" in data
    assert b"ActionState" in data
    assert b"SUCCESS" in data
