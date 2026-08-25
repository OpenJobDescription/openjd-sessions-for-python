# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""RFC 0008 two-scope isolation, in the inner -> hook direction.

The wrap-actions design rests on two strictly separated scopes: the wrapped
action resolves against the INNER entity's own scope, and the hook resolves
against the WRAP environment's own scope plus the ``WrappedAction.*`` overlay.

The wrap -> inner direction is covered by ``test_wrap_task_run.py`` and
``test_wrap_actions.py::TestWrapScopeSeparation``, and so is one part of this
direction: ``test_wrap_actions.py::test_inner_env_let_does_not_leak_into_hook_scope``
already pins that an inner script's *script-level* ``let`` bindings stay out of a
hook's scope -- those live only in the copy ``_build_wrapped_inner_scope`` makes,
so they never leaked.

What was NOT covered, and did leak, is everything the Session writes into the
inner entity's table directly, because the hook used to resolve against that same
table:

- a wrapped task's ``Task.Param.*`` / ``Task.RawParam.*``
- the running step's ``Step.Name``
- the ``extra_let_bindings`` the *inner* environment was entered with

``WrappedStep.Name`` exists in RFC 0008 precisely because ``Step.Name`` is not
meant to be reachable from a hook. openjd-model does not reject any of these
references in an environment script, so this runtime was the only gate.

Every test here asserts on the symbol table the Session actually hands to the
hook's runner, captured at ``_make_env_script_runner``. Resolving a table the
test built itself would only re-assert the test's own construction.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import PurePosixPath
from typing import Any

import pytest

from openjd.expr import SerializedSymbolTable
from openjd.model import ParameterValue, ParameterValueType, SymbolTable
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    Environment as Environment_2023_09,
    EnvironmentActions as EnvironmentActions_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
    StepActions as StepActions_2023_09,
    StepScript as StepScript_2023_09,
)

from openjd.sessions import (
    ActionState,
    PathFormat,
    PathMappingRule,
    Session,
    SessionState,
)


def _noop(python_exe: str) -> Action_2023_09:
    """A do-nothing action that exists on every platform.

    ``true``/``echo`` are not native Windows executables, and every test in this
    file requires its action to actually complete (see ``_run_until_ready``), so
    the suite's ``python_exe`` fixture is used throughout rather than a shell
    builtin.
    """
    return Action_2023_09(
        command=CommandString_2023_09(python_exe),
        args=[ArgString_2023_09("-c"), ArgString_2023_09("pass")],
    )


def _wrap_env(python_exe: str, name: str = "WrapEnv") -> Environment_2023_09:
    """A wrap environment whose three hooks are all no-ops.

    What the hooks print does not matter here: these tests inspect the symbol
    table the Session builds for the hook, which keeps them independent of
    whether a given symbol happens to be spellable in an ArgString under the
    extensions the model was parsed with. Deliberately declares no script-level
    ``let`` bindings, so a positive assertion about a binding can only be
    attributable to ``_seed_wrap_env_scope``.
    """
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onWrapEnvEnter=_noop(python_exe),
                onWrapTaskRun=_noop(python_exe),
                onWrapEnvExit=_noop(python_exe),
            )
        ),
    )


def _echo(python_exe: str, text: str) -> Action_2023_09:
    """An action that prints ``text``.

    Passing the text as an argv element rather than embedding it in the source
    keeps quoting out of the picture. A reference inside ``text`` must resolve
    for the action to start at all, so a test using this proves resolution
    rather than mere presence in a table.
    """
    return Action_2023_09(
        command=CommandString_2023_09(python_exe),
        args=[
            ArgString_2023_09("-c"),
            ArgString_2023_09("import sys; print(sys.argv[1])"),
            ArgString_2023_09(text),
        ],
    )


def _echoing_wrap_env(python_exe: str, text: str, name: str = "WrapEnv") -> Environment_2023_09:
    """A wrap environment whose three hooks all print ``text``."""
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onWrapEnvEnter=_echo(python_exe, text),
                onWrapTaskRun=_echo(python_exe, text),
                onWrapEnvExit=_echo(python_exe, text),
            )
        ),
    )


def _serialized_table(entries: list[dict[str, str]]) -> SerializedSymbolTable:
    """A service-resolved base table in its wire (JSON) form."""
    return SerializedSymbolTable.from_json_str(json.dumps(entries))


def _inner_env(python_exe: str, name: str = "Inner") -> Environment_2023_09:
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(onEnter=_noop(python_exe), onExit=_noop(python_exe))
        ),
    )


def _step_script(python_exe: str) -> StepScript_2023_09:
    return StepScript_2023_09(actions=StepActions_2023_09(onRun=_noop(python_exe)))


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    """Block until the session leaves RUNNING, then require that it got there.

    The trailing assertion is the load-bearing part. Twelve of the assertions in
    this file are on a table that is fully built *before* the hook's subprocess
    starts, so without it a hung or failed action would leave every one of them
    asserting valid-but-unexercised state and passing.
    """
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)
    assert session.state != SessionState.RUNNING, (
        f"session did not leave RUNNING within {timeout_s}s; "
        f"action_status={session.action_status}"
    )


class _HookScopeCapture:
    """Records the symbol table the Session hands to each hook's runner.

    Patches ``_make_env_script_runner`` because its ``symtab`` argument *is* the
    hook's resolution scope -- the runner resolves the hook's command, args,
    timeout and cancelation against it, and evaluates the wrap env's own lets and
    embedded files into it. Anything absent from it is unreachable from a hook.

    ``symtab`` is keyword-only on that method, so it always arrives in
    ``kwargs``; if that ever changed, nothing would be recorded and
    :meth:`table` would fail loudly rather than pass vacuously.

    Note that the captured tables are live objects: the runner mutates the one it
    was given. Assertions therefore see the table as it was *used*, which is
    stronger than as it was handed over.
    """

    def __init__(self, session: Session) -> None:
        self.tables: list[SymbolTable] = []
        self._original = session._make_env_script_runner

        def _capturing(*args: Any, **kwargs: Any) -> Any:
            symtab = kwargs.get("symtab")
            if symtab is not None:
                self.tables.append(symtab)
            return self._original(*args, **kwargs)

        session._make_env_script_runner = _capturing  # type: ignore[method-assign]

    def table(self, index: int, *, expected_count: int) -> SymbolTable:
        """The ``index``-th captured scope, requiring exactly ``expected_count``.

        The count is asserted rather than just indexing from the end: a
        regression that stopped invoking one of the hooks would otherwise leave a
        test silently asserting against a previous hook's table.
        """
        assert (
            len(self.tables) == expected_count
        ), f"expected {expected_count} hook scope(s), captured {len(self.tables)}"
        return self.tables[index]


def _defined(symtab: SymbolTable, name: str) -> bool:
    """Is ``name`` resolvable in ``symtab``?

    ``SymbolTable.__contains__`` tests the backing table, which is exactly the
    set of names both the legacy interpolation path and the EXPR engine resolve
    from -- so this is "resolvable", not merely "present".
    """
    return name in symtab


class TestTaskScopeDoesNotReachTheHook:
    """onWrapTaskRun: the wrapped task's own symbols must not be in the hook's
    scope."""

    @pytest.mark.parametrize("leaked_symbol", ["Task.Param.Frame", "Task.RawParam.Frame"])
    def test_task_parameters_are_not_in_the_hook_scope(
        self, leaked_symbol: str, python_exe: str
    ) -> None:
        # GIVEN: a wrap env active over a task that has parameters
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={
                    "Frame": ParameterValue(type=ParameterValueType.INT, value="42")
                },
                step_name="RenderStep",
            )
            _run_until_ready(session)

            # THEN: the hook cannot see the wrapped task's parameters -- neither
            # the value nor its EXPR type, which would disclose the parameter's
            # existence on its own.
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            hook_scope = capture.table(0, expected_count=1)
            assert not _defined(hook_scope, leaked_symbol)
            assert leaked_symbol not in hook_scope.expr_types

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_running_step_name_is_not_in_the_hook_scope(self, python_exe: str) -> None:
        """A job-level wrap env has no step context of its own, so nothing
        overwrites Step.Name -- which is how the running step's name used to
        survive into the hook."""
        # GIVEN: a wrap env entered with NO step_name
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: a task of a named step runs under it
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="RenderStep",
            )
            _run_until_ready(session)

            # THEN
            hook_scope = capture.table(0, expected_count=1)
            assert not _defined(hook_scope, "Step.Name")
            # ...and the hook still gets the name through the channel RFC 0008
            # provides for it.
            assert hook_scope["WrappedStep.Name"] == "RenderStep"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_wrap_envs_own_step_name_still_reaches_the_hook(self, python_exe: str) -> None:
        """The fix must not cost the wrap env its own enter-time step context."""
        # GIVEN: a wrap env entered as part of "StepA"
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_wrap_env(python_exe), step_name="StepA"
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: a task of a DIFFERENT step runs under it
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="StepB",
            )
            _run_until_ready(session)

            # THEN: the hook sees its OWN step, not the running one.
            hook_scope = capture.table(0, expected_count=1)
            assert hook_scope["Step.Name"] == "StepA"
            assert hook_scope["WrappedStep.Name"] == "StepB"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_session_scope_still_reaches_the_hook(self, python_exe: str) -> None:
        """Job parameters and session symbols are legitimately session scope and
        must survive the rebuild -- values AND their EXPR types."""
        # GIVEN
        with Session(
            session_id=uuid.uuid4().hex,
            job_parameter_values={
                "JobParam": ParameterValue(type=ParameterValueType.STRING, value="jp")
            },
            job_name="MyJob",
        ) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="S",
            )
            _run_until_ready(session)

            # THEN
            hook_scope = capture.table(0, expected_count=1)
            assert hook_scope["Param.JobParam"] == "jp"
            assert hook_scope["RawParam.JobParam"] == "jp"
            assert hook_scope["Job.Name"] == "MyJob"
            assert hook_scope["Session.WorkingDirectory"] == str(session.working_directory)
            # EXPR typing is part of the scope: without it a PATH-typed symbol
            # silently degrades to a plain string inside a hook's expressions.
            assert hook_scope.expr_types["Session.WorkingDirectory"] == (
                ParameterValueType.PATH.value
            )
            assert hook_scope.expr_types["Param.JobParam"] == (ParameterValueType.STRING.value)

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_path_mapping_reaches_the_hook_intact(self, python_exe: str) -> None:
        """A hook resolving Session.PathMappingRulesFile, or calling the EXPR
        host function apply_path_mapping(), must see this session's real rules.

        Both halves matter and neither was covered: the rules FILE (a hook that
        reads it) and the engine's host RULES (a hook that calls
        apply_path_mapping). With the host rules absent the function silently
        becomes the identity -- no error, wrong paths.
        """
        # GIVEN: a session that actually has path mapping rules
        rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/mnt/src"),
                destination_path=PurePosixPath("/mnt/dst"),
            )
        ]
        with Session(
            session_id=uuid.uuid4().hex,
            job_parameter_values={},
            path_mapping_rules=rules,
        ) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="S",
            )
            _run_until_ready(session)

            # THEN
            hook_scope = capture.table(0, expected_count=1)
            assert hook_scope["Session.HasPathMappingRules"] == "true"
            assert hook_scope.expr_types["Session.HasPathMappingRules"] == (
                ParameterValueType.BOOL.value
            )
            assert str(hook_scope["Session.PathMappingRulesFile"]).endswith(".json")
            # The engine's host context must carry the rules, not an empty list.
            assert hook_scope.expr_host_rules, "a hook lost the session's path mapping rules"
            assert len(hook_scope.expr_host_rules) == len(rules)
            assert hook_scope.expr_host_rules == session._expr_host_rules

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)


class TestInnerEnvironmentScopeDoesNotReachTheHook:
    """onWrapEnvEnter / onWrapEnvExit: the INNER environment's enter-time context
    must not be in the hook's scope."""

    @pytest.mark.parametrize("phase", ["enter", "exit"])
    def test_inner_extra_let_bindings_are_not_in_the_hook_scope(
        self, phase: str, python_exe: str
    ) -> None:
        # GIVEN: a wrap env, and an inner env entered with its own step-level
        # bindings and step name
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            inner_id = session.enter_environment(
                environment=_inner_env(python_exe),
                extra_let_bindings=["inner_secret = 'INNER-ONLY'"],
                step_name="InnerStep",
            )
            _run_until_ready(session)
            if phase == "exit":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)

            # THEN: neither the inner env's binding nor its step name is
            # reachable from the hook that intercepted it. Index the hook we
            # care about rather than the most recent capture, so a hook that
            # stopped running cannot pass by inheriting the other's table.
            expected = 2 if phase == "exit" else 1
            hook_scope = capture.table(expected - 1, expected_count=expected)
            assert not _defined(hook_scope, "inner_secret")
            assert not _defined(hook_scope, "Step.Name")
            assert hook_scope["WrappedEnv.Name"] == "Inner"

            if phase == "enter":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    @pytest.mark.parametrize("phase", ["enter", "exit"])
    def test_wrap_envs_own_bindings_still_reach_the_hook(self, phase: str, python_exe: str) -> None:
        """Guard the behaviour `_seed_wrap_env_scope` exists for.

        ``test_wrap_task_run.py::test_env_hooks_resolve_step_level_let_bindings``
        covers the same ground by asserting the hook merely SUCCEEDs; this
        asserts the binding is in the hook's scope with the right value, which is
        what distinguishes "seeded" from "the hook happened not to need it".
        """
        # GIVEN: a wrap env entered WITH step-level bindings
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_wrap_env(python_exe),
                extra_let_bindings=["wrap_secret = 'WRAP-OWN'"],
                step_name="WrapStep",
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            inner_id = session.enter_environment(environment=_inner_env(python_exe))
            _run_until_ready(session)
            if phase == "exit":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)

            # THEN. `let` bindings are stored as the EXPR engine's typed value,
            # so compare the rendered form rather than the object.
            expected = 2 if phase == "exit" else 1
            hook_scope = capture.table(expected - 1, expected_count=expected)
            assert str(hook_scope["wrap_secret"]) == "WRAP-OWN"
            assert hook_scope["Step.Name"] == "WrapStep"

            if phase == "enter":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)


class TestWrappedActionStillResolvesInTheInnerScope:
    """The other direction must be unaffected: the wrapped action still resolves
    against the inner entity's own scope, which is what makes the split useful."""

    def test_wrapped_args_resolve_the_running_tasks_parameters(self, python_exe: str) -> None:
        # GIVEN: a step whose onRun references its own task parameter
        step = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09(python_exe),
                    args=[ArgString_2023_09("frame-{{Task.Param.Frame}}")],
                )
            )
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=step,
                task_parameter_values={
                    "Frame": ParameterValue(type=ParameterValueType.INT, value="42")
                },
                step_name="RenderStep",
            )
            _run_until_ready(session)

            # THEN: the wrapped action resolved with the task's own parameter,
            # even though the hook's scope cannot see it.
            hook_scope = capture.table(0, expected_count=1)
            assert hook_scope["WrappedAction.Args"] == ["frame-42"]
            assert not _defined(hook_scope, "Task.Param.Frame")

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_two_tasks_do_not_bleed_into_each_other(self, python_exe: str) -> None:
        """The hook's scope is rebuilt per action, so one task's parameters must
        not survive into the next task's hook."""
        # GIVEN
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: two tasks run under the same wrap env
            for value in ("1", "2"):
                session.run_task(
                    step_script=_step_script(python_exe),
                    task_parameter_values={
                        "Frame": ParameterValue(type=ParameterValueType.INT, value=value)
                    },
                    step_name="RenderStep",
                )
                _run_until_ready(session)

            # THEN: distinct tables, neither carrying the task's parameters.
            first = capture.table(0, expected_count=2)
            second = capture.table(1, expected_count=2)
            assert first is not second
            for scope in (first, second):
                assert not _defined(scope, "Task.Param.Frame")

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)


class TestHookScopeBuilderUnit:
    """Direct unit coverage of the builder for the one case the end-to-end tests
    cannot reach: a table that has not been through path mapping."""

    def test_missing_path_mapping_symbols_are_tolerated(self, python_exe: str) -> None:
        """Every production caller materializes path mapping first, so the
        membership guard in the builder has no end-to-end trigger. Pin it here
        rather than leave an unexercised branch."""
        # GIVEN a table with no path-mapping symbols at all
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            revision = _step_script(python_exe).revision

            # WHEN / THEN: no KeyError
            hook_symtab = session._build_wrap_hook_scope(revision, SymbolTable())
            assert not _defined(hook_symtab, "Session.PathMappingRulesFile")
            assert not _defined(hook_symtab, "Session.HasPathMappingRules")


class TestResolvedBaseReachesTheHook:
    """The service-resolved base IS hook scope, matching openjd-rs.

    A hook in openjd-rs resolves against the current action's full symbol
    table, which is built with the base — so a hook referencing a name only
    the base defines resolves there. Python built the hook's table with no
    base at all, so the same hook failed. These tests pin the convergence.

    They do NOT relax the isolation this file is otherwise about. The base a
    step's action carries never holds ``Task.Param.*``: the service copies
    only ``Param.*``/``RawParam.*``/``Job.Name``/``Step.Name``/step-level
    ``let`` values into it. The last test here is the control for that.
    """

    def test_base_symbol_resolves_in_a_task_hook(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: a wrap env whose onWrapTaskRun references a base-only name
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_echoing_wrap_env(python_exe, "HOOK={{from_base}}")
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: a task runs under it WITH a base
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="S",
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN: the hook resolved it and ran.
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert any("HOOK=base value" in m for m in caplog.messages)
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["from_base"]) == "base value"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    @pytest.mark.parametrize("phase", ["enter", "exit"])
    def test_base_symbol_resolves_in_an_environment_hook(
        self, phase: str, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The base rides the INNER environment's call, not the wrap env's.

        A wrap environment's own enter is never itself wrapped, so the base
        that reaches an onWrapEnvEnter hook is the one the inner environment
        was entered with — and for onWrapEnvExit, the one its exit was given.
        """
        # GIVEN
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_echoing_wrap_env(python_exe, "HOOK={{from_base}}")
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: an inner env is entered (and exited) WITH a base
            inner_id = session.enter_environment(
                environment=_inner_env(python_exe), resolved_symtab=base
            )
            _run_until_ready(session)
            if phase == "exit":
                session.exit_environment(identifier=inner_id, resolved_symtab=base)
                _run_until_ready(session)

            # THEN: the intercepting hook resolved the base-only name.
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert any("HOOK=base value" in m for m in caplog.messages)
            expected = 2 if phase == "exit" else 1
            hook_scope = capture.table(expected - 1, expected_count=expected)
            assert str(hook_scope["from_base"]) == "base value"

            if phase == "enter":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_wrap_envs_own_base_reaches_a_later_hook(
        self, python_exe: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The second base channel: the wrap env's OWN enter-time base.

        openjd-rs merges the wrap environment's frozen enter-time resolved
        table onto the hook's table, so a hook referencing its own step's
        context resolves there even when the intercepted action carries no
        base at all. Without this the hook passes on openjd-rs and fails here.
        """
        # GIVEN: a wrap env entered WITH a base of its own
        wrap_base = _serialized_table([{"name": "wrap_own", "type": "string", "value": "WRAP-OWN"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_echoing_wrap_env(python_exe, "HOOK={{wrap_own}}"),
                resolved_symtab=wrap_base,
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: a later task runs under it with NO base of its own, so the
            # only channel that can supply the symbol is the stored one.
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="S",
            )
            _run_until_ready(session)

            # THEN
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS
            assert any("HOOK=WRAP-OWN" in m for m in caplog.messages)
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["wrap_own"]) == "WRAP-OWN"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_wrap_envs_own_base_does_not_reach_the_wrapped_action(self, python_exe: str) -> None:
        """The negative control for the change above.

        Re-seeding the wrap env's base must land in the hook's scope only. If
        it reached the wrapped action's scope, a wrap environment could inject
        symbols into the work it wraps. Asserted on the INNER scope object the
        Session actually resolved the wrapped action against, captured as it is
        handed to the hook-scope builder — the same table, live, so this sees
        it as it was used.
        """
        # GIVEN
        wrap_base = _serialized_table([{"name": "wrap_own", "type": "string", "value": "WRAP-OWN"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_wrap_env(python_exe), resolved_symtab=wrap_base
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)
            inner_scopes: list[SymbolTable] = []
            original_builder = session._build_wrap_hook_scope

            def _capturing_builder(
                version: Any, session_symtab: SymbolTable, **kwargs: Any
            ) -> SymbolTable:
                inner_scopes.append(session_symtab)
                return original_builder(version, session_symtab, **kwargs)

            session._build_wrap_hook_scope = _capturing_builder  # type: ignore[method-assign]

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="S",
            )
            _run_until_ready(session)

            # THEN: the hook has it, the wrapped action's own scope does not.
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["wrap_own"]) == "WRAP-OWN"
            assert len(inner_scopes) == 1, "the inner scope was never captured"
            assert not _defined(inner_scopes[0], "wrap_own")

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_fallback_step_context_wins_over_the_stored_base(self, python_exe: str) -> None:
        """Pins the seeding order inside ``_seed_wrap_env_scope``.

        The stored base seeds first and the ``step_name``/``extra_let_bindings``
        fallback overwrites it, because those carry the same values through the
        channel the base does not cover. This is the accepted divergence from
        openjd-rs, which takes the service's value: when the two disagree, this
        runtime takes the locally supplied one.
        """
        # GIVEN: a wrap env entered with BOTH a base Step.Name and the
        # step_name fallback, disagreeing on purpose.
        wrap_base = _serialized_table(
            [{"name": "Step.Name", "type": "string", "value": "FromBase"}]
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=_wrap_env(python_exe),
                step_name="FromFallback",
                resolved_symtab=wrap_base,
            )
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="RunningStep",
            )
            _run_until_ready(session)

            # THEN
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["Step.Name"]) == "FromFallback"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_base_step_name_is_hook_visible(self, python_exe: str) -> None:
        """Deliberate, and the opposite of the no-base case above.

        ``test_running_step_name_is_not_in_the_hook_scope`` pins that the
        *running* step's name does not leak through the session's own
        ``step_name`` channel. A base ``Step.Name`` is a different channel:
        openjd-rs carries it into hook scope, so this is parity, not a leak.
        Pinned so it is not "fixed" back.
        """
        # GIVEN: a base carrying Step.Name, and a wrap env with no step
        # context of its own to overwrite it.
        base = _serialized_table([{"name": "Step.Name", "type": "string", "value": "BaseStep"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={},
                step_name="RunningStep",
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["Step.Name"]) == "BaseStep"
            assert hook_scope["WrappedStep.Name"] == "RunningStep"

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    @pytest.mark.parametrize("leaked_symbol", ["Task.Param.Frame", "Task.RawParam.Frame"])
    def test_task_parameters_stay_out_with_a_base_present(
        self, leaked_symbol: str, python_exe: str
    ) -> None:
        """The control for the change above.

        The plausible mis-implementation — copying the inner entity's session
        table into the hook scope instead of threading the base — passes every
        other test in this class and fails this one.
        """
        # GIVEN
        base = _serialized_table([{"name": "from_base", "type": "string", "value": "base value"}])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=_wrap_env(python_exe))
            _run_until_ready(session)
            capture = _HookScopeCapture(session)

            # WHEN: a task with parameters runs under it, WITH a base
            session.run_task(
                step_script=_step_script(python_exe),
                task_parameter_values={
                    "Frame": ParameterValue(type=ParameterValueType.INT, value="42")
                },
                step_name="RenderStep",
                resolved_symtab=base,
            )
            _run_until_ready(session)

            # THEN: the base arrived, and the task's parameters still did not.
            hook_scope = capture.table(0, expected_count=1)
            assert str(hook_scope["from_base"]) == "base value"
            assert not _defined(hook_scope, leaked_symbol)
            assert leaked_symbol not in hook_scope.expr_types

            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)
