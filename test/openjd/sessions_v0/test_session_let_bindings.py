# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Regression tests for the Session-level handling of EXPR ``let``
bindings (RFC 0005):

- a failing ``extra_let_bindings`` entry on ``enter_environment`` /
  ``exit_environment`` fails the action through the normal callback path
  instead of raising out of the public API;
- ``enter_environment(step_name=...)`` seeds ``Step.Name`` so step-level
  bindings and the environment's actions can reference it (on both the
  enter and exit sides);
- binding-RHS parsing is memoized across applications;
- the unified optional int-or-format-string field resolver
  (``resolve_optional_int_field``) enforces consistent bounds.
"""

from __future__ import annotations

import sys
import time
import uuid
from typing import Any

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    Environment as Environment_2023_09,
    EnvironmentActions as EnvironmentActions_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
    ModelParsingContext as ModelParsingContext_2023_09,
)
from openjd.model._let_bindings import _parse_rhs
from openjd.sessions import ActionState, ActionStatus, Session, SessionState
from openjd.model.v2023_09 import StepScript as StepScript_2023_09
from openjd.sessions._runner_base import (
    MAX_INT_FIELD_VALUE,
    apply_let_bindings,
    resolve_action_arg_values,
    resolve_optional_int_field,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action(command: str, *args: str) -> Action_2023_09:
    return Action_2023_09(
        command=CommandString_2023_09(command),
        args=[ArgString_2023_09(a) for a in args] if args else None,
    )


def _env(name: str, **action_kwargs) -> Environment_2023_09:
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(**action_kwargs),
        ),
    )


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)


def _format_string_field(raw: str) -> Any:
    """Build a FormatString-typed ``timeout`` field value (FEATURE_BUNDLE_1)."""
    context = ModelParsingContext_2023_09(supported_extensions=["FEATURE_BUNDLE_1", "EXPR"])
    action = Action_2023_09.model_validate({"command": "echo", "timeout": raw}, context=context)
    return action.timeout


# ---------------------------------------------------------------------------
# A failing extra `let` binding must FAIL the action via the callback path,
# never raise out of enter_environment()/exit_environment().
# ---------------------------------------------------------------------------


class TestExtraLetBindingFailure:
    def test_enter_environment_failing_binding_fails_action_cleanly(self) -> None:
        # GIVEN: an extra binding referencing an undefined symbol.
        callback_events: list[ActionStatus] = []

        def callback(session_id: str, status: ActionStatus) -> None:
            callback_events.append(status)

        env = _env("Env", onEnter=_action("true"), onExit=_action("true"))
        with Session(
            session_id=uuid.uuid4().hex, job_parameter_values={}, callback=callback
        ) as session:
            # WHEN: entering must not raise.
            identifier = session.enter_environment(
                environment=env,
                extra_let_bindings=["msg = NoSuchSymbol"],
            )
            _run_until_ready(session)

            # THEN: the action failed cleanly, the callback fired, and the
            # environment remains entered-but-failed (exactly as a failing
            # onEnter subprocess leaves it) so cleanup can exit it.
            assert session.state == SessionState.READY_ENDING
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert status.fail_message is not None
            assert "let" in status.fail_message
            assert callback_events and callback_events[-1].state == ActionState.FAILED
            assert identifier in session.environments_entered

            # WHEN: exiting the failed environment re-applies the failing
            # bindings — the exit action must also fail cleanly, not raise.
            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

            # THEN
            assert session.state == SessionState.READY_ENDING
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert identifier not in session.environments_entered


# ---------------------------------------------------------------------------
# enter_environment(step_name=...) seeds Step.Name (RFC 0005 EXPR), for both
# the enter side and the re-applied bindings on the exit side.
# ---------------------------------------------------------------------------


class TestEnterEnvironmentStepName:
    def test_step_name_resolvable_in_bindings_and_actions(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: a step-level binding referencing Step.Name, echoed by both
        # the environment's onEnter and onExit actions.
        env = _env(
            "StepEnv",
            onEnter=_action("echo", "enter:{{ msg }}"),
            onExit=_action("echo", "exit:{{ msg }}"),
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            identifier = session.enter_environment(
                environment=env,
                extra_let_bindings=["msg = 'step is ' + Step.Name"],
                step_name="MyStep",
            )
            _run_until_ready(session)

            # THEN: the enter action ran with the binding resolved.
            assert session.state == SessionState.READY
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("enter:step is MyStep" in m for m in caplog.messages)

            # WHEN: the exit re-applies the bindings — Step.Name must be
            # re-seeded so onExit resolves in the same scope as onEnter.
            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("exit:step is MyStep" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Binding-RHS parsing is memoized: re-applying the same bindings (per task,
# per env enter/exit) must not re-parse through the engine each time.
# ---------------------------------------------------------------------------


class TestLetBindingParseMemoization:
    def test_parse_is_cached_across_applications(self) -> None:
        # GIVEN: apply_let_bindings delegates to the model's single-sourced
        # evaluator, whose RHS parse (_parse_rhs) is memoized.
        _parse_rhs.cache_clear()
        bindings = ["a = 1 + 2", "b = a * 10"]

        # WHEN: the same bindings apply against several symbol tables.
        for _ in range(5):
            symtab = SymbolTable()
            apply_let_bindings(symtab=symtab, let_bindings=bindings)
            # THEN: per-application evaluation is unchanged. (Values are the
            # engine's typed results; compare via their string rendering.)
            assert str(symtab["a"]) == "3"
            assert str(symtab["b"]) == "30"

        # THEN: each unique RHS was parsed exactly once.
        info = _parse_rhs.cache_info()
        assert info.misses == len(bindings)
        assert info.hits == 4 * len(bindings)

    def test_cached_expression_evaluates_against_per_call_symtab(self) -> None:
        # The memoized expression object must hold no symbol-table state.
        _parse_rhs.cache_clear()
        first = SymbolTable()
        first["X"] = 1
        second = SymbolTable()
        second["X"] = 41
        apply_let_bindings(symtab=first, let_bindings=["y = X + 1"])
        apply_let_bindings(symtab=second, let_bindings=["y = X + 1"])
        assert str(first["y"]) == "2"
        assert str(second["y"]) == "42"


# ---------------------------------------------------------------------------
# A let-bound LIST keeps its engine type through the symbol table and
# flattens through whole-field argument resolution (RFC 0005 §1.3.2) —
# regression for the typed round trip of non-string binding values.
# ---------------------------------------------------------------------------


class TestLetBoundListInArgs:
    def test_let_bound_list_flattens_into_args(self) -> None:
        # GIVEN: a step script whose `let` binds a list and whose onRun
        # consumes it as a whole-field argument expression.
        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        script = StepScript_2023_09.model_validate(
            {
                "let": ["files = ['alpha beta', 'gamma']"],
                "actions": {
                    "onRun": {
                        "command": "echo",
                        "args": ["front", "{{ files }}", "back"],
                    }
                },
            },
            context=context,
        )

        # WHEN: the bindings are applied the way the runner applies them,
        # and the args resolve through the shared enforcement helper.
        symtab = SymbolTable()
        apply_let_bindings(symtab=symtab, let_bindings=script.let or [])
        resolved = resolve_action_arg_values(script.actions.onRun.args, symtab)

        # THEN: the stored value survived the engine symbol-table build as a
        # typed list — flattened inline, one argument per element, embedded
        # whitespace preserved — not rendered as a single stringified list.
        assert resolved == ["front", "alpha beta", "gamma", "back"]


# ---------------------------------------------------------------------------
# resolve_optional_int_field: one implementation for the three
# "optional int-or-format-string" call sites, with consistent bounds.
# ---------------------------------------------------------------------------


class TestResolveOptionalIntField:
    def test_none_field_stays_none(self) -> None:
        assert resolve_optional_int_field(None, SymbolTable(), ge=1, description="timeout") is None

    def test_literal_int_passes_through(self) -> None:
        # Literal values were bounds-checked by the static validator at
        # parse time; they pass through unchecked at run time.
        assert resolve_optional_int_field(30, SymbolTable(), ge=1, description="timeout") == 30

    def test_whole_field_null_resolves_to_none(self) -> None:
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = None
        assert resolve_optional_int_field(field, symtab, ge=1, description="timeout") is None

    def test_whole_field_empty_string_rejected(self) -> None:
        # A genuine empty STRING is not null (openjd-rs parity: only an
        # ExprValue::Null result means "field omitted"; an empty string
        # falls through to the integer parse and errors).
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = ""
        with pytest.raises(ValueError, match="timeout must be a positive integer, got ''"):
            resolve_optional_int_field(field, symtab, ge=1, description="timeout")

    def test_resolved_value_below_ge_rejected(self) -> None:
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = 0
        with pytest.raises(ValueError, match="timeout must be a positive integer, got '0'"):
            resolve_optional_int_field(field, symtab, ge=1, description="timeout")

    def test_resolved_value_above_le_rejected(self) -> None:
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = 700
        with pytest.raises(
            ValueError, match="notifyPeriodInSeconds must be between 1 and 600, got '700'"
        ):
            resolve_optional_int_field(
                field, symtab, ge=1, le=600, description="notifyPeriodInSeconds"
            )

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(" 45 ", id="surrounding whitespace"),
            pytest.param("1_0", id="digit-group underscore"),
            pytest.param("\u0661\u0662\u0663", id="non-ascii decimal digits"),
        ],
    )
    def test_lenient_int_spellings_rejected(self, value: str) -> None:
        # Strict ASCII integer grammar (openjd-rs parity): Python's int()
        # accepts all of these spellings, but Rust's str::parse rejects
        # them — accepting them here would be a spec-observable divergence
        # for dynamically resolved timeout/notifyPeriodInSeconds values.
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = value
        with pytest.raises(ValueError, match="timeout must be a positive integer"):
            resolve_optional_int_field(field, symtab, ge=1, description="timeout")

    def test_leading_plus_sign_accepted(self) -> None:
        # Rust's u64/i64 from_str accepts a leading '+'; so do we.
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = "+45"
        assert resolve_optional_int_field(field, symtab, ge=1, description="timeout") == 45

    def test_non_integer_resolved_value_rejected(self) -> None:
        field = _format_string_field("{{ X }}")
        symtab = SymbolTable()
        symtab["X"] = "abc"
        with pytest.raises(ValueError, match="timeout must be a positive integer, got 'abc'"):
            resolve_optional_int_field(field, symtab, ge=1, description="timeout")

    def test_session_resolve_action_timeout_rejects_non_positive(self) -> None:
        # Regression: Session._resolve_action_timeout previously accepted
        # any int() — a resolved non-positive timeout must now be rejected,
        # matching the openjd-rs runtime and the enforcement path in
        # ScriptRunnerBase._run_action.
        context = ModelParsingContext_2023_09(supported_extensions=["FEATURE_BUNDLE_1", "EXPR"])
        action = Action_2023_09.model_validate(
            {"command": "echo", "timeout": "{{ X }}"}, context=context
        )
        symtab = SymbolTable()
        symtab["X"] = 0
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            with pytest.raises(ValueError, match="timeout must be a positive integer"):
                session._resolve_action_timeout(action, symtab)


# ---------------------------------------------------------------------------
# A runtime EXPR failure in an environment's `variables:` map must FAIL the
# action via the callback path, never raise out of enter_environment().
# ---------------------------------------------------------------------------


class TestEnvironmentVariablesRuntimeFailure:
    def test_failing_variable_expression_fails_action_cleanly(self) -> None:
        # GIVEN: a `variables:` entry that passes validation but cannot be
        # evaluated at run time (int() applied to a non-numeric value). Under
        # legacy interpolation this was unreachable; EXPR host functions make it
        # reachable for a template that validated successfully.
        callback_events: list[ActionStatus] = []

        def callback(session_id: str, status: ActionStatus) -> None:
            callback_events.append(status)

        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        environment = Environment_2023_09.model_validate(
            {
                "name": "BrokenVars",
                "variables": {"BROKEN": "{{ int(Session.WorkingDirectory.name) }}"},
                "script": {"actions": {"onEnter": {"command": "echo", "args": ["entered"]}}},
            },
            context=context,
        )

        with Session(
            session_id=uuid.uuid4().hex, job_parameter_values={}, callback=callback
        ) as session:
            # WHEN: entering must not raise.
            identifier = session.enter_environment(environment=environment)
            _run_until_ready(session)

            # THEN: the caller gets the identifier, a terminal FAILED status, and
            # a session it can still exit the attempted environment from.
            assert identifier is not None
            assert session.state == SessionState.READY_ENDING
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert status.fail_message is not None
            assert "environment variables" in status.fail_message
            assert callback_events and callback_events[-1].state == ActionState.FAILED
            assert identifier in session.environments_entered

            # AND: the change record was seeded, so the log-forwarding thread
            # cannot KeyError on an openjd_env emitted by this environment.
            assert identifier in session._created_env_vars

            # WHEN: cleanup exits it
            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

            # THEN
            assert identifier not in session.environments_entered


# ---------------------------------------------------------------------------
# RFC 0005 1.3.2 typed argument semantics on the ENFORCEMENT path: what a real
# subprocess receives, not just what the helper returns. A whole-field list
# expression flattens to one argument per element; a whole-field null is
# skipped entirely.
# ---------------------------------------------------------------------------


class TestTypedArgumentSemanticsEndToEnd:
    def test_subprocess_argv_flattens_lists_and_skips_nulls(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: a step whose args mix plain strings, a whole-field list
        # expression, and a whole-field null.
        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        step = StepScript_2023_09.model_validate(
            {
                "let": ["items = ['alpha beta', 'gamma']", "missing = null"],
                "actions": {
                    "onRun": {
                        "command": sys.executable,
                        "args": [
                            "-c",
                            "import sys; print('ARGV=' + repr(sys.argv[1:]))",
                            "front",
                            "{{items}}",
                            "{{missing}}",
                            "back",
                        ],
                    }
                },
            },
            context=context,
        )

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(step_script=step, task_parameter_values={})
            _run_until_ready(session)

        # THEN: four arguments -- the list flattened element-wise (preserving the
        # space inside an element) and the null argument was dropped. A stringified
        # list or an empty argument would both be wrong.
        status = session.action_status
        assert status is not None
        assert status.state == ActionState.SUCCESS
        expected = "ARGV=" + repr(["front", "alpha beta", "gamma", "back"])
        assert any(expected in m for m in caplog.messages), [
            m for m in caplog.messages if "ARGV=" in m
        ]


# ---------------------------------------------------------------------------
# The over-range bound applies to a value that arrives through a format string,
# not only to a literal -- that is the path a forwarded WrappedAction.Timeout
# takes, and without the bound the action would run unbounded instead of failing.
# ---------------------------------------------------------------------------


class TestOverRangeResolvedIntField:
    def test_resolved_value_above_max_is_rejected(self) -> None:
        # GIVEN
        symtab = SymbolTable()
        symtab["X"] = str(MAX_INT_FIELD_VALUE + 1)

        # WHEN / THEN
        with pytest.raises(ValueError, match="must be at most"):
            resolve_optional_int_field(
                _format_string_field("{{ X }}"), symtab, ge=1, description="timeout"
            )

    def test_resolved_value_at_max_is_accepted(self) -> None:
        # GIVEN
        symtab = SymbolTable()
        symtab["X"] = str(MAX_INT_FIELD_VALUE)

        # WHEN / THEN: the boundary itself is pinned from both sides.
        assert (
            resolve_optional_int_field(
                _format_string_field("{{ X }}"), symtab, ge=1, description="timeout"
            )
            == MAX_INT_FIELD_VALUE
        )


# ---------------------------------------------------------------------------
# Task.File.* / Env.File.* are PATH-typed, so EXPR property access works on
# them. Without the type they resolve as plain strings and `.parent` fails.
# ---------------------------------------------------------------------------


class TestEmbeddedFileSymbolsArePathTyped:
    def test_task_file_supports_path_property_access(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: a step-level binding taking `.parent` of an embedded file path.
        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        step = StepScript_2023_09.model_validate(
            {
                "let": ["where = string(Task.File.Cfg.parent)"],
                "actions": {"onRun": {"command": "echo", "args": ["PARENT={{where}}"]}},
                "embeddedFiles": [{"name": "Cfg", "type": "TEXT", "data": "config-contents\n"}],
            },
            context=context,
        )

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(step_script=step, task_parameter_values={})
            _run_until_ready(session)

            # THEN: the binding resolved, so the symbol carried the PATH type.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("PARENT=" in m and "embedded_files" in m for m in caplog.messages)
