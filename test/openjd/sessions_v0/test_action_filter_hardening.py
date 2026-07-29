# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Hardening of the action-output filter: log redaction, and containment of
consumer callbacks.

``ActionMonitoringFilter`` runs on the thread forwarding a subprocess's stdout,
so anything that escapes it unwinds ``LoggingSubprocess.run()`` and costs us the
output stream and process ownership. It is also the control that keeps secrets
out of the log.
"""

import logging
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    StepActions as StepActions_2023_09,
    StepScript as StepScript_2023_09,
)

from openjd.sessions import ActionState, Session, SessionState
from openjd.sessions._action_filter import (
    ActionMessageKind,
    ActionMonitoringFilter,
    envvar_set_matcher_json,
    envvar_set_matcher_str,
    envvar_unset_matcher,
)


def _make_record(
    msg: str, args: Any = None, session_id: str = "foo", level: int = logging.INFO
) -> logging.LogRecord:
    """A LogRecord shaped the way the session logger produces them."""
    record = logging.LogRecord("test", level, "path", 1, msg, args, None)
    record.session_id = session_id  # type: ignore[attr-defined]
    return record


class _UnrenderableError(Exception):
    """An exception whose rendering raises -- the shape that defeated the R5-2
    containment, which interpolated the exception into an f-string."""

    def __str__(self) -> str:
        raise RuntimeError("__str__ is hostile")

    def __repr__(self) -> str:
        raise RuntimeError("__repr__ is hostile too")


class TestRedactionDoesNotLeakViaRecordArgs:
    """R5-1: `record.args` must be empty by the time the filter returns.

    The redaction logic only ever inspects `record.msg`. A downstream handler
    calls `record.getMessage()`, which re-runs `msg % args` -- so any path that
    leaves `args` populated re-interpolates the *un-scanned* original into the
    emitted line.
    """

    def _filter_with_secret(self, secret: str = "SUPERSECRET") -> ActionMonitoringFilter:
        f = ActionMonitoringFilter(session_id="foo", callback=MagicMock())
        f._redacted_values.add(secret)
        return f

    def test_secret_in_args_is_redacted_when_formatting_succeeds(self) -> None:
        # GIVEN: a secret carried only in args, with a msg that matches nothing
        f = self._filter_with_secret()
        record = _make_record("value is %s", ("SUPERSECRET",))

        # WHEN
        f.filter(record)

        # THEN: the emitted line -- which is what a handler actually renders --
        # carries no secret, and args cannot reintroduce one.
        assert "SUPERSECRET" not in record.getMessage()
        assert not record.args

    def test_secret_in_args_is_redacted_when_formatting_fails(self) -> None:
        """The load-bearing case. `%d` against a str raises, so the old code
        skipped clearing args and the handler re-interpolated the secret."""
        # GIVEN: a record whose own %-formatting is broken
        f = self._filter_with_secret()
        record = _make_record("value is %d", ("SUPERSECRET",))

        # WHEN
        f.filter(record)

        # THEN: args are cleared, so getMessage() cannot resurrect the secret,
        # and the secret does not survive anywhere on the record.
        assert not record.args
        assert "SUPERSECRET" not in record.getMessage()
        assert "SUPERSECRET" not in str(record.msg)

    def test_record_stays_renderable_when_formatting_fails(self) -> None:
        """Folding args in must not leave a record that raises in the handler."""
        # GIVEN
        f = self._filter_with_secret()
        record = _make_record("value is %d", ("SUPERSECRET",))

        # WHEN
        f.filter(record)

        # THEN: getMessage() does not raise (it would if msg still held %d and
        # args were still a str tuple), and the redaction marker is present.
        assert "*" * 8 in record.getMessage()

    def test_whole_line_redaction_clears_args(self) -> None:
        # GIVEN: a message that matches a whole redacted line
        f = ActionMonitoringFilter(session_id="foo", callback=MagicMock())
        f._redacted_lines.add("secret-line")
        record = _make_record("secret-line", None)

        # WHEN
        f.filter(record)

        # THEN
        assert record.getMessage() == "*" * 8
        assert not record.args

    def test_args_are_untouched_when_no_redactions_registered(self) -> None:
        """Lazy %-formatting must keep working for every ordinary log line."""
        # GIVEN: no registered secrets
        f = ActionMonitoringFilter(session_id="foo", callback=MagicMock())
        record = _make_record("value is %s", ("plain",))

        # WHEN
        f.filter(record)

        # THEN: the filter did not fold args in, so the handler still formats.
        assert record.args == ("plain",)
        assert record.getMessage() == "value is plain"


class TestFilterContainsConsumerCallbackFailures:
    """R5-2: this filter runs on the thread forwarding the subprocess's stdout.

    An exception escaping `filter()` unwinds `LoggingSubprocess.run()` before the
    child is waited on: the pump thread dies, the rest of the output is dropped,
    and the process is left unreaped.
    """

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("boom"), KeyError("boom"), TypeError("boom"), AttributeError("boom")],
        ids=["RuntimeError", "KeyError", "TypeError", "AttributeError"],
    )
    def test_handler_callback_exception_does_not_escape(self, exc: Exception) -> None:
        # GIVEN: a consumer callback that raises a non-ValueError
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise exc

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        record = _make_record("openjd_progress: 50.0")

        # WHEN / THEN: filter() returns normally...
        assert f.filter(record) is True
        # ...and keeps the record, with the failure visible in the action's output.
        assert "boom" in record.getMessage()

    def test_malformed_env_callback_exception_does_not_escape(self) -> None:
        """The one callback invocation in filter() not routed through `handler`."""

        # GIVEN
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise RuntimeError("boom")

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        # A near-miss env command: space before the colon.
        record = _make_record("openjd_env : FOO=bar")

        # WHEN / THEN
        assert f.filter(record) is True

    def test_valueerror_still_annotates_the_record(self) -> None:
        """The pre-existing ValueError contract must be unchanged."""
        # GIVEN: progress outside the legal range raises ValueError in the handler
        f = ActionMonitoringFilter(session_id="foo", callback=MagicMock())
        record = _make_record("openjd_progress: 500.0")

        # WHEN / THEN
        assert f.filter(record) is True
        assert "ERROR" in record.getMessage()

    def test_redaction_failure_fails_closed(self) -> None:
        """If the redaction control itself breaks, emit nothing rather than an
        unscanned line -- and do not let it reach the pump thread."""
        # GIVEN
        f = ActionMonitoringFilter(session_id="foo", callback=MagicMock())
        record = _make_record("carries a secret")

        # WHEN
        with patch.object(
            ActionMonitoringFilter,
            "apply_message_redaction",
            side_effect=RuntimeError("redaction is broken"),
        ):
            result = f.filter(record)

        # THEN
        assert result is True
        assert record.getMessage() == "*" * 8

    def test_a_live_child_is_still_reaped_when_the_callback_raises(self) -> None:
        """End to end: the reason R5-2 matters. A progress update from a live
        child must not cost us the process."""
        # GIVEN: a consumer that raises on the first progress update
        state: dict[str, Any] = {"raised": False}

        def callback(session_id: str, status: Any) -> None:
            if status.progress is not None and not state["raised"]:
                state["raised"] = True
                raise RuntimeError("consumer blew up on a progress update")

        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09(sys.executable),
                    args=[
                        ArgString_2023_09("-c"),
                        ArgString_2023_09(
                            "print('openjd_progress: 50.0', flush=True)\n"
                            "print('done', flush=True)\n"
                        ),
                    ],
                )
            )
        )

        # WHEN
        with Session(session_id="r5-2-e2e", job_parameter_values={}, callback=callback) as session:
            session.run_task(step_script=script, task_parameter_values={})
            deadline = 60.0
            import time

            start = time.monotonic()
            while session.state == SessionState.RUNNING and time.monotonic() - start < deadline:
                time.sleep(0.05)

            # THEN: the consumer did raise, the action still reached a terminal
            # state, and the subprocess was waited on -- an exit code proves the
            # `wait()` in LoggingSubprocess.run() was reached rather than skipped.
            assert state["raised"] is True
            assert session.state != SessionState.RUNNING
            assert session.action_status is not None
            assert session.action_status.exit_code == 0
            assert session.action_status.state == ActionState.SUCCESS


class TestEnvVarNameAnchoring:
    """`$` also matches immediately before a trailing newline, so an
    `$`-anchored NAME pattern accepted "FOO\\n" -- a name no OS can hold.

    The VALUE half stays deliberately permissive: a multi-line value delivered
    through the JSON form is supported, tested behaviour.
    """

    @pytest.mark.parametrize("name", ["FOO\n", "FOO\r", "FOO\r\n"])
    def test_unset_rejects_a_trailing_newline_in_the_name(self, name: str) -> None:
        assert envvar_unset_matcher.match(name) is None

    def test_unset_still_accepts_a_legal_name(self) -> None:
        assert envvar_unset_matcher.match("FOO_BAR9") is not None

    @pytest.mark.parametrize("payload", ["FOO=bar\n", "FOO\n=bar"])
    def test_set_rejects_a_trailing_newline(self, payload: str) -> None:
        assert envvar_set_matcher_str.match(payload) is None

    def test_multiline_value_via_json_is_still_supported(self) -> None:
        """Guards the intended feature against over-correction."""
        # GIVEN / WHEN
        raw = '"FOO=BAR\\nBAZ"'
        # THEN: the raw (escaped) form still validates...
        assert envvar_set_matcher_json.match(raw) is not None
        # ...and the decoded multi-line value still reaches the callback.
        callback = MagicMock()
        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        f.filter(_make_record('openjd_env: "FOO=BAR\\nBAZ"'))
        env_calls = [
            c
            for c in callback.call_args_list
            if c[0][0] == ActionMessageKind.ENV and isinstance(c[0][1], dict)
        ]
        assert len(env_calls) == 1
        assert env_calls[0][0][1] == {"name": "FOO", "value": "BAR\nBAZ"}

    @pytest.mark.parametrize(
        "msg",
        [
            'openjd_env: "FOO\\nBAR=baz"',
            'openjd_env: "FOO\\u000aBAR=baz"',
            'openjd_env: "FOO\\u0000BAR=baz"',
        ],
    )
    def test_a_separator_cannot_reach_a_decoded_name(self, msg: str) -> None:
        """A name carrying a separator must be rejected outright, not passed on.

        Rewritten after an audit: the previous version looped over the recorded
        calls asserting a property of any dict it found, and since these inputs
        produce no ENV dict at all, its assertion body never executed. It passed
        against a deliberately broken implementation. Assert the rejection
        directly instead.
        """
        # GIVEN
        callback = MagicMock()
        f = ActionMonitoringFilter(session_id="foo", callback=callback)

        # WHEN
        f.filter(_make_record(msg))

        # THEN: no environment variable was defined, and the failure was reported.
        env_defs = [
            c
            for c in callback.call_args_list
            if c[0][0] == ActionMessageKind.ENV and isinstance(c[0][1], dict)
        ]
        assert env_defs == []
        assert callback.call_args_list, "the parse failure must be reported to the consumer"
        assert callback.call_args_list[-1][0][2] is True  # cancel-and-fail


class TestContainmentDoesNotReRaise:
    def test_handler_path_contains_an_unrenderable_exception(self) -> None:
        # GIVEN: a consumer callback raising an exception that cannot be rendered
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise _UnrenderableError()

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        record = _make_record("openjd_progress: 50.0")

        # WHEN / THEN: filter() still returns rather than letting the exception
        # reach the stdout pump thread.
        assert f.filter(record) is True
        assert "_UnrenderableError" in record.getMessage()

    def test_malformed_env_path_contains_an_unrenderable_exception(self) -> None:
        # GIVEN
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise _UnrenderableError()

        f = ActionMonitoringFilter(session_id="foo", callback=callback)

        # WHEN / THEN
        assert f.filter(_make_record("openjd_env : FOO=bar")) is True

    def test_renderable_exceptions_still_report_their_message(self) -> None:
        """The defensive rendering must not degrade the ordinary case."""

        # GIVEN
        def callback(kind: ActionMessageKind, value: Any, fail: bool) -> None:
            raise RuntimeError("a perfectly ordinary boom")

        f = ActionMonitoringFilter(session_id="foo", callback=callback)
        record = _make_record("openjd_progress: 50.0")

        # WHEN
        f.filter(record)

        # THEN
        assert "a perfectly ordinary boom" in record.getMessage()
