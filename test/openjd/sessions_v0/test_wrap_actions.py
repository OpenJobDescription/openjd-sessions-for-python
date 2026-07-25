# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""End-to-end tests for the three RFC 0008 wrap hooks
(``onWrapEnvEnter``, ``onWrapTaskRun``, ``onWrapEnvExit``) and the
single-wrap-layer validation.

The tests use the marker-file pattern from the RFC: each action
appends a tagged line to a shared file in the session working
directory, and the final contents prove which actions ran via which
path. No containers required — ``echo`` and ``cat`` only.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

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
from openjd.sessions import ActionState, ActionStatus, Session, SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action(command: str, *args: str) -> Action_2023_09:
    return Action_2023_09(
        command=CommandString_2023_09(command),
        args=[ArgString_2023_09(a) for a in args] if args else None,
    )


def _env(name: str, **action_kwargs) -> Environment_2023_09:
    """Build an environment whose script defines whichever hooks the
    caller passes (e.g. ``onEnter=…``, ``onWrapTaskRun=…``)."""
    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(**action_kwargs),
        ),
    )


def _step(command: str, *args: str) -> StepScript_2023_09:
    return StepScript_2023_09(actions=StepActions_2023_09(onRun=_action(command, *args)))


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)


def _trace_path(session: Session) -> Path:
    return Path(str(session.working_directory)) / "trace.log"


_NOOP = _action("true")


# ---------------------------------------------------------------------------
# Single-wrap-layer enforcement (RFC 0008: at most one wrap-defining
# environment in the session stack).
# ---------------------------------------------------------------------------


class TestSingleWrapLayer:
    def test_two_wrap_envs_rejected_at_enter(self) -> None:
        outer = _env(
            "outer",
            onWrapEnvEnter=_NOOP,
            onWrapTaskRun=_action("sh", "-c", "echo outer-wrap"),
            onWrapEnvExit=_NOOP,
        )
        inner = _env(
            "inner",
            onWrapEnvEnter=_action("sh", "-c", "echo inner-wrap"),
            onWrapTaskRun=_NOOP,
            onWrapEnvExit=_NOOP,
        )

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(environment=outer)
            _run_until_ready(session)

            with pytest.raises(RuntimeError, match=r"RFC 0008"):
                session.enter_environment(environment=inner)

    def test_two_envs_with_only_one_wrap_layer_ok(self) -> None:
        outer = _env(
            "outer",
            onWrapEnvEnter=_NOOP,
            onWrapTaskRun=_action("sh", "-c", "echo outer-wrap"),
            onWrapEnvExit=_NOOP,
        )
        # The inner env defines no wrap hooks — fine to enter.
        inner = _env("inner", onEnter=_action("sh", "-c", "echo inner-onEnter"))

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(environment=outer)
            _run_until_ready(session)
            session.enter_environment(environment=inner)
            _run_until_ready(session)
            assert session.state == SessionState.READY


# ---------------------------------------------------------------------------
# onWrapEnvEnter intercepts inner onEnter (RFC Test 1).
# ---------------------------------------------------------------------------


class TestWrapEnvEnter:
    def test_wrap_env_enter_intercepts_inner_on_enter(self) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            trace = _trace_path(session)
            outer = _env(
                "outer",
                onWrapEnvEnter=_action(
                    "sh",
                    "-c",
                    f"echo '[WRAPPED] inner-onEnter' >> {trace} && "
                    f"echo 'inner-onEnter body' >> {trace}",
                ),
                onWrapTaskRun=_NOOP,
                onWrapEnvExit=_NOOP,
                onEnter=_action("sh", "-c", f"echo 'outer-onEnter' >> {trace}"),
            )
            inner = _env(
                "inner",
                onEnter=_action("sh", "-c", f"echo 'should not run on host' >> {trace}"),
            )

            session.enter_environment(environment=outer)
            _run_until_ready(session)
            session.enter_environment(environment=inner)
            _run_until_ready(session)

            assert session.state == SessionState.READY
            assert trace.exists()
            content = trace.read_text()
            # The outer env's own onEnter ran on host.
            assert "outer-onEnter" in content
            # The wrap script ran in place of the inner onEnter.
            assert "[WRAPPED] inner-onEnter" in content
            assert "inner-onEnter body" in content
            # The inner env's host-side onEnter did NOT run.
            assert "should not run on host" not in content

    def test_wrap_env_enter_receives_wrapped_symbols(self) -> None:
        """``WrappedEnv.Name`` and ``WrappedAction.Command`` resolve to the
        inner env's identity inside the wrap script."""
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            trace = _trace_path(session)
            outer = _env(
                "outer",
                onWrapEnvEnter=_action(
                    "sh",
                    "-c",
                    "echo "
                    "'name={{WrappedEnv.Name}} cmd={{WrappedAction.Command}}' "
                    f">> {trace}",
                ),
                onWrapTaskRun=_NOOP,
                onWrapEnvExit=_NOOP,
            )
            inner = _env("inner-env-name", onEnter=_action("echo", "hello"))

            session.enter_environment(environment=outer)
            _run_until_ready(session)
            session.enter_environment(environment=inner)
            _run_until_ready(session)

            assert session.state == SessionState.READY
            content = trace.read_text()
            assert "name=inner-env-name" in content
            assert "cmd=echo" in content


# ---------------------------------------------------------------------------
# onWrapEnvExit intercepts inner onExit (RFC Test 2).
# ---------------------------------------------------------------------------


class TestWrapEnvExit:
    def test_wrap_env_exit_intercepts_inner_on_exit(self) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            trace = _trace_path(session)
            outer = _env(
                "outer",
                onWrapEnvEnter=_NOOP,
                onWrapTaskRun=_NOOP,
                onWrapEnvExit=_action(
                    "sh",
                    "-c",
                    f"echo '[WRAPPED] inner-onExit' >> {trace}",
                ),
                onExit=_action("sh", "-c", f"echo 'outer-onExit' >> {trace}"),
            )
            inner = _env(
                "inner",
                onExit=_action("sh", "-c", f"echo 'should not run on host' >> {trace}"),
            )

            outer_id = session.enter_environment(environment=outer)
            _run_until_ready(session)
            inner_id = session.enter_environment(environment=inner)
            _run_until_ready(session)

            session.exit_environment(identifier=inner_id)
            _run_until_ready(session)

            content = trace.read_text()
            assert "[WRAPPED] inner-onExit" in content
            assert "should not run on host" not in content

            # The outer env's own onExit must still run on host when we
            # eventually exit it.
            session.exit_environment(identifier=outer_id)
            _run_until_ready(session)
            content = trace.read_text()
            assert "outer-onExit" in content


# ---------------------------------------------------------------------------
# Visible end-to-end ordering across all three hooks (RFC Test 5).
# ---------------------------------------------------------------------------


class TestVisibleOrdering:
    def test_full_ordering_across_three_hooks(self) -> None:
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            trace = _trace_path(session)
            outer = _env(
                "outer",
                onEnter=_action("sh", "-c", f"echo 'outer-onEnter' >> {trace}"),
                onWrapEnvEnter=_action("sh", "-c", f"echo '[WRAPPED] inner-onEnter' >> {trace}"),
                onWrapTaskRun=_action("sh", "-c", f"echo '[WRAPPED] task-onRun' >> {trace}"),
                onWrapEnvExit=_action("sh", "-c", f"echo '[WRAPPED] inner-onExit' >> {trace}"),
                onExit=_action("sh", "-c", f"echo 'outer-onExit' >> {trace}"),
            )
            wrapped_inner = _env(
                "wrapped-inner",
                onEnter=_action("sh", "-c", f"echo 'inner-onEnter body' >> {trace}"),
                onExit=_action("sh", "-c", f"echo 'inner-onExit body' >> {trace}"),
            )
            step = _step("echo", "task-onRun-body")

            outer_id = session.enter_environment(environment=outer)
            _run_until_ready(session)
            wrapped_id = session.enter_environment(environment=wrapped_inner)
            _run_until_ready(session)

            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            session.exit_environment(identifier=wrapped_id)
            _run_until_ready(session)
            session.exit_environment(identifier=outer_id)
            _run_until_ready(session)

            lines = [line for line in trace.read_text().splitlines() if line.strip()]
            # The exact order matters here — RFC 0008's pass criterion.
            assert lines[0] == "outer-onEnter"
            assert lines[1] == "[WRAPPED] inner-onEnter"
            assert lines[2] == "[WRAPPED] task-onRun"
            assert lines[3] == "[WRAPPED] inner-onExit"
            assert lines[4] == "outer-onExit"


# ---------------------------------------------------------------------------
# WrappedAction.* injection: inner embedded files, and failure handling.
#
# openjd-rs #277: the wrapped entity's embedded files ARE materialized on
# the wrap path (into the inner scope only), so a wrapped action
# referencing {{Task.File.*}} / {{Env.File.*}} resolves to a real on-disk
# path in WrappedAction.Command. Injection failures that remain possible
# (e.g. an undefined symbol) must still FAIL the action through the normal
# callback path instead of raising out of the public API, and the session
# transitions to READY_ENDING (never stuck in RUNNING).
# ---------------------------------------------------------------------------


def _embedded_file_step() -> StepScript_2023_09:
    from openjd.model.v2023_09 import (
        DataString as DataString_2023_09,
        EmbeddedFileText as EmbeddedFileText_2023_09,
        EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
    )

    return StepScript_2023_09(
        actions=StepActions_2023_09(
            onRun=Action_2023_09(command=CommandString_2023_09("{{ Task.File.Foo }}"))
        ),
        embeddedFiles=[
            EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                runnable=True,
                data=DataString_2023_09("#!/bin/sh\necho hello\n"),
            )
        ],
    )


def _embedded_file_env(name: str) -> Environment_2023_09:
    from openjd.model.v2023_09 import (
        DataString as DataString_2023_09,
        EmbeddedFileText as EmbeddedFileText_2023_09,
        EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
    )

    return Environment_2023_09(
        name=name,
        script=EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(command=CommandString_2023_09("{{ Env.File.Setup }}")),
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Setup",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    runnable=True,
                    data=DataString_2023_09("#!/bin/sh\necho setup\n"),
                )
            ],
        ),
    )


class TestWrapInjectionFailure:
    def _wrap_env(self) -> Environment_2023_09:
        return _env(
            "Wrapper",
            onWrapEnvEnter=_action("echo", "{{WrappedAction.Command}}"),
            onWrapTaskRun=_action("echo", "{{WrappedAction.Command}}"),
            onWrapEnvExit=_NOOP,
        )

    def test_run_task_with_embedded_file_command_resolves(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # openjd-rs #277: a wrapped onRun referencing {{Task.File.*}} now
        # resolves — the step's embedded files are materialized into the
        # inner scope, and WrappedAction.Command carries the on-disk path.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(environment=self._wrap_env())
            _run_until_ready(session)
            assert session.state == SessionState.READY

            session.run_task(
                step_script=_embedded_file_step(),
                task_parameter_values={},
                step_name="Step",
            )
            _run_until_ready(session)

            assert session.state == SessionState.READY
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            # The wrap hook echoed WrappedAction.Command: the materialized
            # file path inside the session's files directory.
            messages = "\n".join(caplog.messages)
            assert str(session.files_directory) in messages

    def test_enter_environment_with_embedded_file_on_enter_resolves(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # openjd-rs #277: a wrapped onEnter referencing {{Env.File.*}} now
        # resolves against the inner environment's own materialized files.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=self._wrap_env())
            _run_until_ready(session)

            inner_id = session.enter_environment(environment=_embedded_file_env("Inner"))
            _run_until_ready(session)

            assert session.state == SessionState.READY
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS
            messages = "\n".join(caplog.messages)
            assert str(session.files_directory) in messages
            session.exit_environment(identifier=inner_id, keep_session_running=True)
            _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_run_task_with_unresolvable_wrapped_symbol_fails_action(self) -> None:
        # The graceful-failure contract still holds for injection failures:
        # a wrapped onRun referencing an undefined symbol must FAIL the
        # action via the callback path, not raise out of run_task().
        bad_step = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=CommandString_2023_09("{{ Task.File.DoesNotExist }}"))
            ),
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(environment=self._wrap_env())
            _run_until_ready(session)
            assert session.state == SessionState.READY

            session.run_task(
                step_script=bad_step,
                task_parameter_values={},
                step_name="Step",
            )
            _run_until_ready(session)

            assert session.state == SessionState.READY_ENDING
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            assert status.fail_message is not None
            assert "onWrapTaskRun" in status.fail_message


class TestWrapScopeSeparation:
    """openjd-rs #277: the wrapper's and the wrapped entity's scopes must be
    kept strictly apart. WrappedAction.* resolves against the INNER scope
    only; the hook script resolves against the WRAP environment's own scope
    (including its script-level ``let`` bindings) only."""

    def test_same_let_name_each_side_sees_own_value(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: the wrapper and the step both bind the same `let` name
        # `who` to different values. The step's onRun args reference
        # {{who}}; the hook also references {{who}} directly.
        wrapper = Environment_2023_09(
            name="Wrapper",
            script=EnvironmentScript_2023_09(
                let=["who = 'wrapper-scope'"],
                actions=EnvironmentActions_2023_09(
                    onWrapEnvEnter=_NOOP,
                    onWrapTaskRun=_action(
                        "sh",
                        "-c",
                        "echo HOOK-WHO={{who}} WRAPPED-ARGS={{WrappedAction.Args}}",
                    ),
                    onWrapEnvExit=_NOOP,
                ),
            ),
        )
        step = StepScript_2023_09(
            let=["who = 'step-scope'"],
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=CommandString_2023_09("echo"),
                    args=[ArgString_2023_09("{{who}}")],
                )
            ),
        )

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(environment=wrapper)
            _run_until_ready(session)

            # WHEN: the task runs through the wrap hook.
            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            # THEN: the hook saw the WRAPPER's binding, and the wrapped
            # action's args resolved with the STEP's binding.
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        messages = "\n".join(caplog.messages)
        assert "HOOK-WHO=wrapper-scope" in messages, messages
        # WrappedAction.Args is a list; its default rendering brackets the
        # single resolved element.
        assert "WRAPPED-ARGS=[step-scope]" in messages, messages

    def test_inner_env_let_does_not_leak_into_hook_scope(self) -> None:
        # An inner environment's `let` binding must resolve in
        # WrappedAction.* but must NOT be visible to the hook script: a
        # hook referencing it directly fails the action.
        wrapper = _env(
            "Wrapper",
            onWrapEnvEnter=_action("sh", "-c", "echo LEAKED={{inner_only}}"),
            onWrapTaskRun=_NOOP,
            onWrapEnvExit=_NOOP,
        )
        inner = Environment_2023_09(
            name="Inner",
            script=EnvironmentScript_2023_09(
                let=["inner_only = 'secret'"],
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=CommandString_2023_09("{{inner_only}}")),
                ),
            ),
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=wrapper)
            _run_until_ready(session)

            inner_id = session.enter_environment(environment=inner)
            _run_until_ready(session)

            # The hook referenced {{inner_only}}, which is not in its scope.
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.FAILED
            session.exit_environment(identifier=inner_id, keep_session_running=True)
            _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

    def test_wrapped_env_enter_command_resolves_inner_let(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The inner environment's `let` binding must be visible to
        # WrappedAction.* on the onWrapEnvEnter path.
        wrapper = _env(
            "Wrapper",
            onWrapEnvEnter=_action("sh", "-c", "echo INNERCMD={{WrappedAction.Command}}"),
            onWrapTaskRun=_NOOP,
            onWrapEnvExit=_NOOP,
        )
        inner = Environment_2023_09(
            name="Inner",
            script=EnvironmentScript_2023_09(
                let=["tool = 'inner-tool'"],
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=CommandString_2023_09("{{tool}}")),
                ),
            ),
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(environment=wrapper)
            _run_until_ready(session)

            inner_id = session.enter_environment(environment=inner)
            _run_until_ready(session)

            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            session.exit_environment(identifier=inner_id, keep_session_running=True)
            _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)
        messages = "\n".join(caplog.messages)
        assert "INNERCMD=inner-tool" in messages, messages


class TestWrappedActionEnvironmentContents:
    def test_variables_map_included_in_wrapped_environment(self) -> None:
        # RFC 0008 (openjd-rs #277): WrappedAction.Environment carries every
        # session-defined variable — openjd_env definitions AND entered
        # environments' declarative variables: maps. Host-inherited
        # variables remain excluded.
        from openjd.model.v2023_09 import (
            EnvironmentVariableValueString as EnvironmentVariableValueString_2023_09,
        )

        declaring = Environment_2023_09(
            name="Declaring",
            variables={
                "DECLARED_VAR": EnvironmentVariableValueString_2023_09("from-variables-map")
            },
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(
                environment=_env(
                    "Wrapper",
                    onWrapEnvEnter=_NOOP,
                    onWrapTaskRun=_NOOP,
                    onWrapEnvExit=_NOOP,
                )
            )
            _run_until_ready(session)
            session.enter_environment(environment=declaring)
            _run_until_ready(session)

            env_list = session._collect_session_env_list()
            assert any(entry == "DECLARED_VAR=from-variables-map" for entry in env_list), env_list

    def test_later_set_overrides_earlier_value_without_duplicate(self) -> None:
        # Cumulative flattening: when a later environment's openjd_env sets
        # a name an earlier environment already set, the list carries only
        # the effective (later) value — matching the real subprocess env
        # and the Rust runtime's single cumulative map.
        from openjd.sessions._session import (
            EnvironmentVariableSetChange,
            EnvironmentVariableUnsetChange,
            SimplifiedEnvironmentVariableChanges,
        )

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            scenarios: list[
                tuple[str, list[EnvironmentVariableSetChange | EnvironmentVariableUnsetChange]]
            ] = [
                ("env-a", [EnvironmentVariableSetChange(name="FOO", value="1")]),
                ("env-b", [EnvironmentVariableSetChange(name="FOO", value="2")]),
            ]
            for env_id, changes in scenarios:
                session._environments_entered.append(env_id)
                tracked = SimplifiedEnvironmentVariableChanges({})
                tracked.simplify_ordered_changes(changes)
                session._created_env_vars[env_id] = tracked

            assert session._collect_session_env_list() == ["FOO=2"]

            # And a later unset removes the name entirely.
            session._environments_entered.append("env-c")
            tracked = SimplifiedEnvironmentVariableChanges({})
            tracked.simplify_ordered_changes([EnvironmentVariableUnsetChange(name="FOO")])
            session._created_env_vars["env-c"] = tracked

            assert session._collect_session_env_list() == []

            # Clear the fake stack so Session.cleanup() doesn't try to
            # unwind environments that were never really entered.
            session._environments_entered.clear()
            session._created_env_vars.clear()

    def test_exit_path_includes_exiting_envs_openjd_env_vars(self) -> None:
        # On the onWrapEnvExit path, WrappedAction.Environment must include
        # the exiting environment's own openjd_env variables: the real
        # subprocess env (and the unwrapped onExit) includes them, so the
        # list must match. The wrapper's onWrapEnvEnter emits the
        # openjd_env message, which is attributed to the inner (wrapped)
        # environment being entered.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            trace = _trace_path(session)
            wrapper = _env(
                "Wrapper",
                onWrapEnvEnter=_action("sh", "-c", "echo 'openjd_env: INNER_VAR=inner-value'"),
                onWrapTaskRun=_NOOP,
                onWrapEnvExit=_action(
                    "sh",
                    "-c",
                    f"echo \"ENVLIST=<{{{{WrappedAction.Environment}}}}>\" >> '{trace}'",
                ),
            )
            inner = _env("Inner", onEnter=_NOOP, onExit=_NOOP)

            wrap_id = session.enter_environment(environment=wrapper)
            _run_until_ready(session)
            inner_id = session.enter_environment(environment=inner)
            _run_until_ready(session)
            session.exit_environment(identifier=inner_id, keep_session_running=True)
            _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

            contents = trace.read_text()
            assert "INNER_VAR=inner-value" in contents, contents

    def test_openjd_env_vars_included_in_wrapped_environment(self) -> None:
        # openjd_env-emitted variables must still be surfaced. The wrapper's
        # own onEnter is never wrapped, so it can emit the message directly.
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            session.enter_environment(
                environment=_env(
                    "Wrapper",
                    onEnter=_action("sh", "-c", "echo 'openjd_env: EMITTED_VAR=from-openjd-env'"),
                    onWrapEnvEnter=_NOOP,
                    onWrapTaskRun=_NOOP,
                    onWrapEnvExit=_NOOP,
                )
            )
            _run_until_ready(session)

            env_list = session._collect_session_env_list()
            assert "EMITTED_VAR=from-openjd-env" in env_list


class TestRunWrapHookGuard:
    def test_unknown_hook_name_raises(self) -> None:
        from openjd.sessions._runner_env_script import EnvironmentScriptRunner
        import logging

        from openjd.sessions._logging import LoggerAdapter
        from openjd.model import SymbolTable

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            runner = EnvironmentScriptRunner(
                logger=LoggerAdapter(logging.getLogger(__name__), extra={}),
                session_working_directory=Path(str(session.working_directory)),
                environment_script=None,
                symtab=SymbolTable(),
                session_files_directory=Path(str(session.files_directory)),
            )
            with pytest.raises(ValueError, match="Unknown wrap hook name"):
                runner._run_wrap_hook("onWrapTypo")
