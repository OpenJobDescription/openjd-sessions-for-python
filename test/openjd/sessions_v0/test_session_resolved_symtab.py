# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the ``resolved_symtab`` parameter on the v0 ``Session``'s
``enter_environment`` / ``exit_environment`` / ``run_task``.

The parameter mirrors the ``_v1`` (Rust-backed) session: a
``SerializedSymbolTable`` resolved by the service at CreateJob
(``Param.*``, ``RawParam.*``, ``Job.Name``, ``Step.Name``, step-level
``let`` values) seeds the session symbol table first, and the session's
own values layer on top — the layering the openjd-rs runtime applies to
the same table:

- runtime locals (``Session.WorkingDirectory``, path-mapped ``Param.*``)
  overwrite what the base carries;
- ``Job.Name`` from the base wins over the constructor value;
- script-scope ``let`` shadows base symbols (child table);
- a base that fails entry validation fails the action through the normal
  callback path, never raising out of the public API.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from openjd.expr import SerializedSymbolTable
from openjd.model import ParameterValue, ParameterValueType
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    Environment as Environment_2023_09,
    EnvironmentActions as EnvironmentActions_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
    ModelParsingContext as ModelParsingContext_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import (
    ActionState,
    ActionStatus,
    PathFormat,
    PathMappingRule,
    Session,
    SessionState,
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


def _serialized_table(entries: list[dict[str, str]]) -> SerializedSymbolTable:
    """Build a SerializedSymbolTable from its wire (JSON) form — the same
    shape the service serves as ``resolvedSymbolTable``. Types are the
    lowercase engine names (string, int, float, bool, path)."""
    return SerializedSymbolTable.from_json_str(json.dumps(entries))


def _expr_step_script(payload: dict) -> StepScript_2023_09:
    """Parse a step script with the EXPR extension enabled, needed for
    expression interpolation and script-scope ``let``."""
    context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
    return StepScript_2023_09.model_validate(payload, context=context)


# ---------------------------------------------------------------------------
# run_task: base symbols resolve, with the session's own values on top.
# ---------------------------------------------------------------------------


class TestRunTaskResolvedSymtab:
    def test_base_only_symbol_resolves_in_action(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: a step script referencing a name defined only by the
        # service-resolved base (no template `let` anywhere) — the headline
        # equivalence case with the Rust runtime.
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "task:{{ from_base }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            assert session.state == SessionState.READY
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("task:base value" in m for m in caplog.messages)

    def test_base_int_keeps_its_type_for_arithmetic(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: an int in the base, consumed by an arithmetic expression.
        # Pins type fidelity: the base entry arrives as a typed ExprValue,
        # not a string, or `v + 5` would fail to evaluate.
        script = _expr_step_script(
            {"actions": {"onRun": {"command": "echo", "args": ["result:", "{{ v + 5 }}"]}}}
        )
        base = _serialized_table([{"name": "v", "type": "int", "value": "10"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("result: 15" in m for m in caplog.messages)

    def test_job_name_comes_from_base_when_ctor_omits_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: no constructor job_name; the base carries Job.Name — the
        # channel job-template-scope values ride in on.
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "job:{{ Job.Name }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table([{"name": "Job.Name", "type": "string", "value": "BaseJob"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("job:BaseJob" in m for m in caplog.messages)

    def test_base_job_name_wins_over_ctor(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: both a constructor job_name and a base Job.Name. The base
        # wins: in openjd-rs Job.Name rides the base and is never re-set,
        # and both values come from the service anyway.
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "job:{{ Job.Name }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table([{"name": "Job.Name", "type": "string", "value": "BaseJob"}])
        with Session(
            session_id=uuid.uuid4().hex, job_parameter_values={}, job_name="CtorJob"
        ) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("job:BaseJob" in m for m in caplog.messages)
            assert not any("job:CtorJob" in m for m in caplog.messages)

    def test_runtime_locals_override_base(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: the base carries a bogus Session.WorkingDirectory. The
        # session's own value must layer over it — runtime locals are the
        # session's to set, exactly as in the Rust layering.
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "wd:{{ Session.WorkingDirectory }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table(
            [{"name": "Session.WorkingDirectory", "type": "path", "value": "/bogus/nowhere"}]
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN: the real working directory, not the base's value.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            expected = f"wd:{session.working_directory}"
            assert any(expected in m for m in caplog.messages)
            assert not any("/bogus/nowhere" in m for m in caplog.messages)

    def test_path_param_remapped_over_base(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: a session with a path mapping rule, and a base carrying the
        # UNMAPPED path for a PATH job parameter (the base is serialized
        # before host rules are known). The session re-seeds Param.* from
        # its own values with mapping applied — the v0 counterpart of
        # Rust's step 2 re-mapping.
        if os.name == "nt":
            rule = PathMappingRule(
                source_path_format=PathFormat.WINDOWS,
                source_path=PureWindowsPath(r"c:\source"),
                destination_path=PureWindowsPath(r"c:\dest"),
            )
            unmapped, mapped = r"c:\source\file.txt", r"c:\dest\file.txt"
        else:
            rule = PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/source"),
                destination_path=PurePosixPath("/dest"),
            )
            unmapped, mapped = "/source/file.txt", "/dest/file.txt"
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "p:{{ Param.P }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table([{"name": "Param.P", "type": "path", "value": unmapped}])
        with Session(
            session_id=uuid.uuid4().hex,
            job_parameter_values={
                "P": ParameterValue(type=ParameterValueType.PATH, value=unmapped)
            },
            path_mapping_rules=[rule],
        ) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN: the mapped path, not the base's unmapped one.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any(f"p:{mapped}" in m for m in caplog.messages)
            assert not any(f"p:{unmapped}" in m for m in caplog.messages)

    def test_script_scope_let_shadows_base_symbol(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: the same name in the base and in the script's own `let`.
        # The narrower (script) scope must win — the runner evaluates script
        # bindings into a child table sourced from the session-scope one.
        script = _expr_step_script(
            {
                "let": ["shared = 'from script'"],
                "actions": {"onRun": {"command": "echo", "args": ["task:{{ shared }}"]}},
            }
        )
        base = _serialized_table([{"name": "shared", "type": "string", "value": "from base"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("task:from script" in m for m in caplog.messages)
            assert not any("task:from base" in m for m in caplog.messages)

    def test_extra_let_bindings_coexist_with_base(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: both channels at once — the base (serving gate open) and
        # extra_let_bindings (the fallback channel). Both must resolve.
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "a:{{ from_base }}", "b:{{ from_let }}")},  # type: ignore[arg-type]
        )
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=base,
                extra_let_bindings=["from_let = 'let value'"],
            )
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("a:base value" in m for m in caplog.messages)
            assert any("b:let value" in m for m in caplog.messages)

    def test_omitting_the_parameter_changes_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: the negative control. The parameter is additive and
        # optional, so a task that does not use it must behave exactly as
        # before — this is what makes the change safe for every caller.
        script = _expr_step_script(
            {
                "let": ["own = 'script only'"],
                "actions": {"onRun": {"command": "echo", "args": ["task:{{ own }}"]}},
            }
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            session.run_task(step_script=script, task_parameter_values={})
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("task:script only" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# enter_environment / exit_environment accept and use the base.
# ---------------------------------------------------------------------------


class TestEnvironmentResolvedSymtab:
    def test_enter_and_exit_use_the_base(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: an environment whose onEnter and onExit reference a name
        # defined only by the base — the worker passes the same table to
        # both sides so onExit resolves in the same scope as onEnter.
        env = _env(
            "BaseEnv",
            onEnter=_action("echo", "enter:{{ from_base }}"),
            onExit=_action("echo", "exit:{{ from_base }}"),
        )
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN
            identifier = session.enter_environment(environment=env, resolved_symtab=base)
            _run_until_ready(session)

            # THEN: the enter action resolved the base symbol.
            assert session.state == SessionState.READY
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("enter:base value" in m for m in caplog.messages)

            # WHEN
            session.exit_environment(identifier=identifier, resolved_symtab=base)
            _run_until_ready(session)

            # THEN: the exit action resolved it too.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            assert any("exit:base value" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# An invalid base fails the action cleanly, never raising out of the
# public API — the same contract the extra `let` bindings failure holds.
# ---------------------------------------------------------------------------


class TestInvalidResolvedSymtab:
    def test_invalid_base_fails_action_cleanly(self) -> None:
        # GIVEN: a base that is well-formed JSON with invalid entry
        # contents. from_json_str only checks JSON well-formedness; entry
        # validation happens lazily in to_symtab (raising ValueError) at
        # the action boundary.
        callback_events: list[ActionStatus] = []

        def callback(session_id: str, status: ActionStatus) -> None:
            callback_events.append(status)

        bad_base = _serialized_table([{"name": "v", "type": "bogus", "value": "5"}])
        script = StepScript_2023_09(
            actions={"onRun": _action("echo", "unreachable")},  # type: ignore[arg-type]
        )
        with Session(
            session_id=uuid.uuid4().hex, job_parameter_values={}, callback=callback
        ) as session:
            # WHEN: this must not raise.
            session.run_task(
                step_script=script,
                task_parameter_values={},
                resolved_symtab=bad_base,
            )
            _run_until_ready(session)

            # THEN: the action failed through the callback path.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert status.fail_message is not None
            assert "resolved symbol table" in status.fail_message
            assert callback_events and callback_events[-1].state == ActionState.FAILED

        env = _env("Env", onEnter=_action("true"), onExit=_action("true"))
        with Session(
            session_id=uuid.uuid4().hex, job_parameter_values={}, callback=callback
        ) as session:
            # WHEN: entering must not raise either.
            identifier = session.enter_environment(environment=env, resolved_symtab=bad_base)
            _run_until_ready(session)

            # THEN: the action failed cleanly and the environment remains
            # entered-but-failed (exactly as a failing onEnter subprocess
            # leaves it) so cleanup can exit it.
            assert session.state == SessionState.READY_ENDING
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert status.fail_message is not None
            assert "resolved symbol table" in status.fail_message
            assert identifier in session.environments_entered

            # WHEN: exiting the failed environment (without a base) works.
            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

            # THEN
            assert identifier not in session.environments_entered
