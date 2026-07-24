# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for ``WrappedAction.Cancelation.*`` injection (RFC 0008 follow-up,
openjd-specifications#148).

``WrappedAction.Cancelation.Mode`` is ``string?`` and carries the wrapped
action's cancelation method — ``"TERMINATE"``, ``"NOTIFY_THEN_TERMINATE"``,
or ``None`` when the wrapped action defines no ``<Cancelation>``. The null
case is deliberately distinct from an explicit ``TERMINATE``.

``WrappedAction.Cancelation.NotifyPeriodInSeconds`` is ``int?``: the
effective grace period when the mode is ``NOTIFY_THEN_TERMINATE`` (with the
Template Schemas 5.3.2 defaults applied — 120 for a task's ``onRun``, 30
otherwise), and ``None`` when a notify period does not apply.

Mirrors the Rust integration coverage in openjd-rs
``tests/integration/test_wrap_actions.rs`` and the conformance fixtures
``conformance-tests/2023-09/WRAP_ACTIONS/jobs/wrap-cancelation-*``.
"""

from __future__ import annotations

import time
import uuid

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CancelationMethodNotifyThenTerminate as NotifyThenTerminate_2023_09,
    CancelationMethodTerminate as Terminate_2023_09,
    CancelationMode as CancelationMode_2023_09,
    CommandString as CommandString_2023_09,
    Environment as Environment_2023_09,
    EnvironmentActions as EnvironmentActions_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
    StepActions as StepActions_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import ActionState, ActionStatus, Session, SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NOOP = Action_2023_09(command=CommandString_2023_09("true"))

_TERMINATE = Terminate_2023_09(mode=CancelationMode_2023_09.TERMINATE)


def _notify(period: int | None) -> NotifyThenTerminate_2023_09:
    return NotifyThenTerminate_2023_09(
        mode=CancelationMode_2023_09.NOTIFY_THEN_TERMINATE,
        notifyPeriodInSeconds=period,
    )


def _step_script_with_cancelation(cancelation) -> StepScript_2023_09:
    return StepScript_2023_09(
        actions=StepActions_2023_09(
            onRun=Action_2023_09(
                command=CommandString_2023_09("echo"),
                args=[ArgString_2023_09("placeholder")],
                cancelation=cancelation,
            )
        )
    )


def _wrap_env(name: str, wrap_action: Action_2023_09) -> Environment_2023_09:
    """Build an Environment with ``onWrapTaskRun`` set to ``wrap_action``
    and the other two wrap hooks set to no-ops (all-or-nothing rule)."""
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onWrapEnvEnter=_NOOP,
                onWrapTaskRun=wrap_action,
                onWrapEnvExit=_NOOP,
            ),
        ),
    )


def _inner_env_action(cancelation) -> Action_2023_09:
    return Action_2023_09(
        command=CommandString_2023_09("inner-enter-cmd"),
        args=[ArgString_2023_09("--flag")],
        cancelation=cancelation,
    )


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Unit tests — task-run injection path
# ---------------------------------------------------------------------------


class TestInjectTaskCancelationSymbols:
    """Unit tests for cancelation injection via ``_inject_wrapped_task_symbols``."""

    def _inject(self, cancelation) -> SymbolTable:
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            script = _step_script_with_cancelation(cancelation)
            session._inject_wrapped_task_symbols(symtab, script, "Step1", inner_symtab=symtab)
            return symtab
        finally:
            session.cleanup()

    def test_mode_none_and_period_none_when_no_cancelation(self) -> None:
        # No <Cancelation> declared: Mode is None (NOT "TERMINATE") and the
        # notify period is None — the string?/int? null values.
        symtab = self._inject(None)
        assert symtab["WrappedAction.Cancelation.Mode"] is None
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] is None

    def test_mode_terminate_and_period_none(self) -> None:
        symtab = self._inject(_TERMINATE)
        assert symtab["WrappedAction.Cancelation.Mode"] == "TERMINATE"
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] is None

    def test_notify_then_terminate_with_explicit_period(self) -> None:
        symtab = self._inject(_notify(45))
        assert symtab["WrappedAction.Cancelation.Mode"] == "NOTIFY_THEN_TERMINATE"
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] == 45

    def test_notify_then_terminate_defaults_to_120_for_task_on_run(self) -> None:
        # Template Schemas 5.3.2: the default notify period for a task's
        # onRun is 120 seconds. The runtime supplies the value it would have
        # enforced in the unwrapped case.
        symtab = self._inject(_notify(None))
        assert symtab["WrappedAction.Cancelation.Mode"] == "NOTIFY_THEN_TERMINATE"
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] == 120


# ---------------------------------------------------------------------------
# Unit tests — env enter/exit injection path
# ---------------------------------------------------------------------------


class TestInjectEnvCancelationSymbols:
    """Unit tests for cancelation injection via ``_inject_wrapped_env_symbols``."""

    def _inject(self, cancelation) -> SymbolTable:
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            inner_env = Environment_2023_09(
                name="InnerEnv",
                script=EnvironmentScript_2023_09(
                    actions=EnvironmentActions_2023_09(
                        onEnter=_inner_env_action(cancelation),
                    ),
                ),
            )
            session._inject_wrapped_env_symbols(
                symtab, inner_env, _inner_env_action(cancelation), inner_symtab=symtab
            )
            return symtab
        finally:
            session.cleanup()

    def test_notify_then_terminate_defaults_to_30_for_env_action(self) -> None:
        # Template Schemas 5.3.2: for anything other than a task's onRun —
        # including an inner environment's onEnter — the default is 30.
        symtab = self._inject(_notify(None))
        assert symtab["WrappedAction.Cancelation.Mode"] == "NOTIFY_THEN_TERMINATE"
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] == 30

    def test_explicit_period_forwards_verbatim_for_env_action(self) -> None:
        symtab = self._inject(_notify(45))
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] == 45

    def test_mode_none_when_env_action_has_no_cancelation(self) -> None:
        symtab = self._inject(None)
        assert symtab["WrappedAction.Cancelation.Mode"] is None
        assert symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] is None


# ---------------------------------------------------------------------------
# Integration tests — real subprocess, format-string interpolation
# ---------------------------------------------------------------------------


class TestWrapCancelationExecution:
    """End-to-end: the wrap action interpolates the Cancelation variables in
    a format string. The ``<...>`` sentinel wrap makes the null-renders-empty
    behavior observable (``NP=<>``), matching the conformance fixtures."""

    _PROBE = Action_2023_09(
        command=CommandString_2023_09("sh"),
        args=[
            ArgString_2023_09("-c"),
            ArgString_2023_09(
                'echo "MODE=<{{WrappedAction.Cancelation.Mode}}>";'
                ' echo "NP=<{{WrappedAction.Cancelation.NotifyPeriodInSeconds}}>"'
            ),
        ],
    )

    def _run(self, cancelation, caplog: pytest.LogCaptureFixture) -> str:
        session_id = uuid.uuid4().hex
        env = _wrap_env("wrap_env", self._PROBE)
        step = _step_script_with_cancelation(cancelation)
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=env)
            _run_until_ready(session)
            session.run_task(step_script=step, task_parameter_values={})
            _run_until_ready(session)
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        return "\n".join(caplog.messages)

    def test_terminate_mode_renders_null_period_as_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        messages = self._run(_TERMINATE, caplog)
        assert "MODE=<TERMINATE>" in messages
        # int? null interpolates as the empty string (RFC 0005), so the
        # sentinel wrap renders as NP=<> — distinct from the old 0 sentinel,
        # which would have rendered NP=<0>.
        assert "NP=<>" in messages

    def test_notify_then_terminate_default_renders_120(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        messages = self._run(_notify(None), caplog)
        assert "MODE=<NOTIFY_THEN_TERMINATE>" in messages
        assert "NP=<120>" in messages

    def test_no_cancelation_renders_null_mode_as_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No <Cancelation> declared: Mode is null (string?), which
        # interpolates as the empty string (RFC 0005) — MODE=<>, matching
        # the wrap-cancelation-mode-null-when-no-cancelation conformance
        # fixture. The nullness itself (vs. an empty string value) is
        # asserted by the unit tests above and observable via EXPR
        # null-coalescing in the conformance fixture.
        messages = self._run(None, caplog)
        assert "MODE=<>" in messages
        assert "NP=<>" in messages


# ---------------------------------------------------------------------------
# Unit tests — deferred-mode resolution (resolve_effective_cancelation)
# ---------------------------------------------------------------------------


class TestResolveEffectiveCancelation:
    """Unit tests for the shared deferred-cancelation resolution helper.

    A CancelationMethodDeferred carries a format-string mode whose
    TERMINATE-vs-NOTIFY_THEN_TERMINATE decision is made at run time
    against the live symbol table (see resolve_effective_cancelation's
    docstring for the full story).
    """

    def _deferred(self, mode: str, period: str | None = None):
        from openjd.model._format_strings import FormatString
        from openjd.model.v2023_09 import CancelationMethodDeferred, ModelParsingContext

        # A deferred mode is an RFC 0008 forwarding construct, and
        # WRAP_ACTIONS requires the EXPR extension — so the format strings
        # here parse as EXPR expressions, giving the typed null semantics
        # the runtime relies on (a whole-field null drops the cancelation
        # object; an empty STRING is an error, matching openjd-rs).
        ctx = ModelParsingContext(supported_extensions=["FEATURE_BUNDLE_1", "EXPR"])
        return CancelationMethodDeferred(
            mode=FormatString(mode, context=ctx),
            notifyPeriodInSeconds=(
                FormatString(period, context=ctx) if period is not None else None
            ),
        )

    def _symtab(self, **values) -> SymbolTable:
        symtab = SymbolTable()
        for key, value in values.items():
            symtab[key] = value
        return symtab

    def test_mode_resolving_null_drops_whole_object(self) -> None:
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}", "{{P}}")
        result = resolve_effective_cancelation(cancelation, self._symtab(X=None, P=None))
        assert result == (None, None)

    def test_mode_resolving_terminate(self) -> None:
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}")
        result = resolve_effective_cancelation(cancelation, self._symtab(X="TERMINATE"))
        assert result == ("TERMINATE", None)

    def test_mode_resolving_terminate_rejects_non_null_period(self) -> None:
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}", "{{P}}")
        with pytest.raises(ValueError, match="does not accept"):
            resolve_effective_cancelation(cancelation, self._symtab(X="TERMINATE", P=45))

    def test_mode_resolving_notify_then_terminate_with_period(self) -> None:
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}", "{{P}}")
        result = resolve_effective_cancelation(
            cancelation, self._symtab(X="NOTIFY_THEN_TERMINATE", P=45)
        )
        assert result == ("NOTIFY_THEN_TERMINATE", 45)

    def test_whole_field_mode_resolving_empty_string_raises(self) -> None:
        # A genuine empty STRING is not null, even for a whole-field
        # expression (openjd-rs parity: only an ExprValue::Null result
        # drops the cancelation object; an empty string is an invalid
        # mode). E.g. a STRING parameter whose value is "".
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}")
        with pytest.raises(ValueError, match="must resolve to .* got ''"):
            resolve_effective_cancelation(cancelation, self._symtab(X=""))

    def test_mode_resolving_garbage_raises(self) -> None:
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}")
        with pytest.raises(ValueError, match="must resolve to"):
            resolve_effective_cancelation(cancelation, self._symtab(X="SOMETHING_ELSE"))

    def test_partial_interpolation_mode_resolves_normally(self) -> None:
        # Normal format string behavior (Template Schemas 5.3): partial
        # interpolation is permitted; the resolved value is checked
        # against the two mode names.
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}_THEN_TERMINATE", "{{P}}")
        result = resolve_effective_cancelation(cancelation, self._symtab(X="NOTIFY", P=45))
        assert result == ("NOTIFY_THEN_TERMINATE", 45)

    def test_partial_interpolation_mode_resolving_empty_raises(self) -> None:
        # Null semantics (dropping the cancelation object) apply only to a
        # whole-field expression. A normal format string that resolves to
        # the empty string is not null — it is an invalid mode.
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}{{Y}}")
        with pytest.raises(ValueError, match="must resolve to"):
            resolve_effective_cancelation(cancelation, self._symtab(X=None, Y=None))

    def test_resolved_period_exceeding_cap_raises(self) -> None:
        # The static validator caps literal periods at 600 (Template
        # Schemas 5.3.2); format-string values could not be checked at
        # parse time, so the resolved value is bounds-checked at run time.
        from openjd.sessions._runner_base import resolve_effective_cancelation

        cancelation = self._deferred("{{X}}", "{{P}}")
        with pytest.raises(ValueError, match="between 1 and 600"):
            resolve_effective_cancelation(
                cancelation, self._symtab(X="NOTIFY_THEN_TERMINATE", P=9999)
            )


# ---------------------------------------------------------------------------
# Launch-time cancelation resolution (openjd-rs run_action parity):
# the effective cancel method is resolved by _run_action against the SAME
# final scope the command/args resolved with (script lets, *.File.*,
# WrappedAction.*) and stored on the runner; cancel() consumes it. An
# unresolvable or invalid cancelation fails the action at start.
# ---------------------------------------------------------------------------


class TestLaunchTimeCancelationResolution:
    def _env_with_let_bound_cancelation(self) -> Environment_2023_09:
        from openjd.model.v2023_09 import ModelParsingContext

        ctx = ModelParsingContext(supported_extensions=["EXPR", "WRAP_ACTIONS", "FEATURE_BUNDLE_1"])
        return Environment_2023_09.model_validate(
            {
                "name": "Wrapper",
                "script": {
                    "let": [
                        "hookMode = 'NOTIFY_THEN_TERMINATE'",
                        "hookPeriod = 9",
                    ],
                    "actions": {
                        "onEnter": {"command": "true"},
                        "onWrapEnvEnter": {"command": "true"},
                        "onWrapEnvExit": {"command": "true"},
                        "onWrapTaskRun": {
                            "command": "sleep",
                            "args": ["20"],
                            "cancelation": {
                                "mode": "{{hookMode}}",
                                "notifyPeriodInSeconds": "{{hookPeriod}}",
                            },
                        },
                    },
                },
            },
            context=ctx,
        )

    def test_let_bound_cancelation_resolved_against_final_scope(self) -> None:
        # Regression: cancel() used to re-resolve the cancelation against
        # the runner's BASE symtab — which lacks the script's `let`
        # bindings — so a let-referencing mode fell back to Terminate with
        # a warning. It must resolve at launch, in the hook's final scope.
        from datetime import timedelta
        from unittest.mock import MagicMock, patch

        from openjd.sessions._runner_env_script import EnvironmentScriptRunner
        from openjd.sessions._runner_base import NotifyCancelMethod

        env = self._env_with_let_bound_cancelation()
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            with patch.object(EnvironmentScriptRunner, "_run"):
                runner = EnvironmentScriptRunner(
                    logger=MagicMock(),
                    session_working_directory=tmp_path,
                    environment_script=env.script,
                    symtab=SymbolTable(),
                    session_files_directory=tmp_path,
                )
                runner.wrap_task_run()
                assert runner._resolved_cancel_method == NotifyCancelMethod(
                    terminate_delay=timedelta(seconds=9)
                )

    def test_invalid_deferred_mode_fails_action_at_launch(self) -> None:
        # Eager validation (openjd-rs parity): a cancelation whose deferred
        # mode resolves to something other than the two method names or
        # null must FAIL the action at start — not launch successfully and
        # only surface if a cancel later occurs.
        session_id = uuid.uuid4().hex
        probe = Action_2023_09(
            command=CommandString_2023_09("sh"),
            args=[ArgString_2023_09("-c"), ArgString_2023_09("echo should-not-run")],
        )
        env = _wrap_env("wrap_env", probe)
        step = _step_script_with_cancelation(None)
        # Forward an invalid mode through the wrap round trip: the wrap
        # action's own cancelation defers to a symbol that resolves to
        # garbage at run time.
        from openjd.model._format_strings import FormatString
        from openjd.model.v2023_09 import CancelationMethodDeferred, ModelParsingContext

        ctx = ModelParsingContext(supported_extensions=["FEATURE_BUNDLE_1", "EXPR"])
        object.__setattr__(
            env.script.actions.onWrapTaskRun,
            "cancelation",
            CancelationMethodDeferred(
                mode=FormatString("{{WrappedStep.Name}}", context=ctx),
                notifyPeriodInSeconds=None,
            ),
        )
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=env)
            _run_until_ready(session)
            session.run_task(step_script=step, task_parameter_values={}, step_name="NotAMode")
            _run_until_ready(session)
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert session.state == SessionState.READY_ENDING
