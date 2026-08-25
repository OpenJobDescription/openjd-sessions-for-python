# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the ``onWrapTaskRun`` environment action (RFC 0008).

These tests exercise the end-to-end behaviour that a job template's ``onRun`` is
intercepted and the active environment's ``onWrapTaskRun`` is executed in its
place, with ``WrappedAction.Command``, ``WrappedAction.Args``,
``WrappedAction.Environment``, ``WrappedAction.Timeout``, and
``WrappedStep.Name`` injected into the wrap action's symbol table.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from openjd.expr import SerializedSymbolTable
from openjd.model import SymbolTable
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


_NOOP = Action_2023_09(command=CommandString_2023_09("true"))


def _serialized_table(entries: list[dict[str, str]]) -> SerializedSymbolTable:
    """A service-resolved base table in its wire (JSON) form."""
    return SerializedSymbolTable.from_json_str(json.dumps(entries))


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


def _step_script(command: str, args: list[str]) -> StepScript_2023_09:
    """Build a minimal StepScript that runs ``command`` with ``args``."""
    return StepScript_2023_09(
        actions=StepActions_2023_09(
            onRun=Action_2023_09(
                command=CommandString_2023_09(command),
                args=[ArgString_2023_09(a) for a in args] if args else None,
            )
        )
    )


def _run_until_ready(session: Session, timeout_s: float = 10.0) -> None:
    """Block until the session transitions back to READY or the timeout elapses."""
    deadline = time.time() + timeout_s
    while session.state == SessionState.RUNNING and time.time() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Unit tests on the pure helpers — no subprocess needed
# ---------------------------------------------------------------------------


class TestInjectWrappedTaskSymbols:
    """Unit tests for the ``_inject_wrapped_task_symbols`` helper."""

    def test_injects_wrapped_command_and_args_as_list(self) -> None:
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            script = _step_script("python3", ["-c", "print('hi')"])
            session._inject_wrapped_task_symbols(symtab, script, "MyStep", inner_symtab=symtab)

            assert symtab["WrappedAction.Command"] == "python3"
            assert symtab["WrappedAction.Args"] == ["-c", "print('hi')"]
            assert isinstance(symtab["WrappedAction.Args"], list)
            assert symtab["WrappedStep.Name"] == "MyStep"
        finally:
            session.cleanup()

    def test_injects_empty_args_when_step_has_no_args(self) -> None:
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            script = _step_script("whoami", [])
            session._inject_wrapped_task_symbols(symtab, script, "Step1", inner_symtab=symtab)

            assert symtab["WrappedAction.Command"] == "whoami"
            assert symtab["WrappedAction.Args"] == []
        finally:
            session.cleanup()

    def test_injects_typed_args_null_skip_and_list_flatten(self) -> None:
        # RFC 0005 §1.3.2 typed argument semantics (openjd-rs parity: the
        # wrapped path in seed_wrapped_action_symbols resolves through the
        # same resolve_action_args as the runner): a whole-field list
        # expression flattens inline (one argument per element), a
        # whole-field null is skipped, and the hook sees exactly the argv
        # the wrapped action would have run with unwrapped.
        from openjd.model.v2023_09 import ModelParsingContext
        from openjd.sessions._runner_base import resolve_action_arg_values

        context = ModelParsingContext(supported_extensions=["EXPR"])
        script = StepScript_2023_09.model_validate(
            {
                "actions": {
                    "onRun": {
                        "command": "echo",
                        "args": ["front", '{{ ["a", "b c"] }}', "{{ null }}", "back"],
                    }
                }
            },
            context=context,
        )
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            session._inject_wrapped_task_symbols(symtab, script, "MyStep", inner_symtab=symtab)
            assert symtab["WrappedAction.Args"] == ["front", "a", "b c", "back"]
            # The unwrapped enforcement path (_run_action) resolves the same
            # action's args via the same shared helper — wrapped and
            # unwrapped runs of this action use identical argv.
            assert resolve_action_arg_values(script.actions.onRun.args, symtab) == [
                "front",
                "a",
                "b c",
                "back",
            ]
        finally:
            session.cleanup()

    def test_injects_wrapped_environment_as_key_value_list(self) -> None:
        # RFC 0008 (openjd-rs #277): WrappedAction.Environment carries every
        # session-defined variable — openjd_env definitions (applied via
        # simplify_ordered_changes) AND the declarative variables:-map seed.
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            # Seeded through the session-lifetime map that now backs this symbol.
            # Previously this fabricated an entry in `_environments_entered` plus
            # a `_created_env_vars` record, which is the per-entered view that
            # feeds the child *process* environment -- not this symbol. That
            # bypassed the production writers, which is why it kept passing while
            # WrappedAction.Environment violated RFC 0008's session-lifetime MUST.
            session._session_env_vars.update(
                {"DECLARED": "from-variables-map", "FOO": "bar", "BAZ": "qux"}
            )

            symtab = SymbolTable()
            script = _step_script("echo", ["hi"])
            session._inject_wrapped_task_symbols(symtab, script, "Step1", inner_symtab=symtab)

            task_env = symtab["WrappedAction.Environment"]
            assert isinstance(task_env, list)
            normalized = {item.upper() for item in task_env}
            # Both openjd_env-set variables and the variables:-map seed are
            # surfaced (openjd-rs #277).
            assert normalized == {
                "DECLARED=from-variables-map".upper(),
                "FOO=bar".upper(),
                "BAZ=qux".upper(),
            }
        finally:
            session.cleanup()

    def test_injects_timeout_none_when_no_timeout(self) -> None:
        # WrappedAction.Timeout is int? (RFC 0008): None (null) when the
        # wrapped action specifies no timeout, so whole-field forwarding
        # (timeout: "{{WrappedAction.Timeout}}") drops the field.
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            script = _step_script("echo", ["hi"])
            session._inject_wrapped_task_symbols(symtab, script, "Step1", inner_symtab=symtab)

            assert symtab["WrappedAction.Timeout"] is None
        finally:
            session.cleanup()

    def test_find_wrap_environment_returns_none_when_no_env_has_wrap(self) -> None:
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            assert session._find_wrap_environment(hook="onWrapTaskRun") is None
        finally:
            session.cleanup()


# ---------------------------------------------------------------------------
# Integration tests — actually run a subprocess wrap action
# ---------------------------------------------------------------------------


class TestWrapTaskRunExecution:
    """Integration tests that enter an environment with ``onWrapTaskRun`` and
    run a task through it, verifying the wrap action is what actually ran."""

    def test_no_wrap_runs_original_step(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: a session with no environment defining onWrapTaskRun.
        session_id = uuid.uuid4().hex
        step = _step_script("echo", ["original-step"])

        # WHEN: we run the task.
        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            # THEN: the original step ran, not a wrap action.
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        assert any("original-step" in msg for msg in caplog.messages)

    def test_wrap_action_intercepts_step(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: an environment that defines onWrapTaskRun.
        session_id = uuid.uuid4().hex
        wrap = Action_2023_09(
            command=CommandString_2023_09("sh"),
            args=[ArgString_2023_09("-c"), ArgString_2023_09("echo WRAPPED-RAN")],
        )
        env = _wrap_env("container_env", wrap)

        step = _step_script("echo", ["original-step"])

        with Session(session_id=session_id, job_parameter_values={}) as session:
            # Enter the wrap env (no onEnter, nothing runs but the env becomes active).
            session.enter_environment(environment=env)
            _run_until_ready(session)

            # WHEN: we run a task.
            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            # THEN: the wrap action ran, not the original step.
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        messages = "\n".join(caplog.messages)
        assert "WRAPPED-RAN" in messages
        assert "original-step" not in messages

    def test_wrap_action_receives_wrapped_command_symbol(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN: a wrap action that echoes WrappedAction.Command via a format string.
        session_id = uuid.uuid4().hex
        wrap = Action_2023_09(
            command=CommandString_2023_09("sh"),
            args=[
                ArgString_2023_09("-c"),
                ArgString_2023_09("echo CMD={{WrappedAction.Command}}"),
            ],
        )
        env = _wrap_env("container_env", wrap)
        step = _step_script("maya-batch", ["-render", "scene.ma"])

        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=env)
            _run_until_ready(session)

            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        messages = "\n".join(caplog.messages)
        assert "CMD=maya-batch" in messages

    def test_innermost_environment_wins(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN: two environments — only the inner one defines onWrapTaskRun.
        session_id = uuid.uuid4().hex
        outer = Environment_2023_09(
            name="outer",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=CommandString_2023_09("sh"),
                        args=[
                            ArgString_2023_09("-c"),
                            ArgString_2023_09("echo outer-enter"),
                        ],
                    )
                )
            ),
        )
        inner = _wrap_env(
            "inner",
            Action_2023_09(
                command=CommandString_2023_09("sh"),
                args=[ArgString_2023_09("-c"), ArgString_2023_09("echo INNER-WRAP")],
            ),
        )
        step = _step_script("echo", ["original"])

        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=outer)
            _run_until_ready(session)
            session.enter_environment(environment=inner)
            _run_until_ready(session)

            session.run_task(step_script=step, task_parameter_values={}, step_name="Step")
            _run_until_ready(session)

            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
        messages = "\n".join(caplog.messages)
        assert "INNER-WRAP" in messages
        assert "original" not in messages

    def test_wrap_action_runs_multiple_tasks(self, caplog: pytest.LogCaptureFixture) -> None:
        # Regression: symbols are re-injected cleanly per task.
        session_id = uuid.uuid4().hex
        wrap = Action_2023_09(
            command=CommandString_2023_09("sh"),
            args=[
                ArgString_2023_09("-c"),
                ArgString_2023_09("echo RAN={{WrappedAction.Command}}"),
            ],
        )
        env = _wrap_env("env", wrap)

        with Session(session_id=session_id, job_parameter_values={}) as session:
            session.enter_environment(environment=env)
            _run_until_ready(session)

            session.run_task(
                step_script=_step_script("cmd-one", []),
                task_parameter_values={},
                step_name="Step",
            )
            _run_until_ready(session)
            session.run_task(
                step_script=_step_script("cmd-two", []),
                task_parameter_values={},
                step_name="Step",
            )
            _run_until_ready(session)

        messages = "\n".join(caplog.messages)
        assert "RAN=cmd-one" in messages
        assert "RAN=cmd-two" in messages


# ---------------------------------------------------------------------------
# Security and execution constraints — RFC 0008 test matrix
# ---------------------------------------------------------------------------
#
# These tests cover the 8 scenarios the RFC calls out under "Recommended test
# cases for implementation". They verify that the symbol injection layer
# preserves WrappedAction.Command and WrappedAction.Args byte-for-byte — no
# shell expansion, no quoting transformation, no truncation.


class TestSecurityAndExecutionConstraints:
    """RFC 0008 §"Recommended test cases for implementation" matrix."""

    def _inject(self, command: str, args: list[str]) -> tuple[str, list[str]]:
        """Helper: run the injection and return the resolved WrappedAction.Command/Args."""
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            symtab = SymbolTable()
            script = _step_script(command, args)
            session._inject_wrapped_task_symbols(symtab, script, "TestStep", inner_symtab=symtab)
            return symtab["WrappedAction.Command"], symtab["WrappedAction.Args"]
        finally:
            session.cleanup()

    # --- 1. Nested quoting ------------------------------------------------

    def test_nested_quoting_preserved(self) -> None:
        # echo "O'Reilly's Guide" — both " and ' must survive injection.
        cmd, args = self._inject("echo", ["O'Reilly's Guide"])
        assert cmd == "echo"
        assert args == ["O'Reilly's Guide"]

    def test_double_quotes_preserved(self) -> None:
        cmd, args = self._inject("python3", ["-c", 'print("hello world")'])
        assert cmd == "python3"
        assert args == ["-c", 'print("hello world")']

    # --- 2. Shell metacharacters ------------------------------------------

    def test_shell_metacharacters_preserved(self) -> None:
        # `, $, |, &&, ;, >, <, *, ?, (, ), \, all in one arg.
        nasty = "$(cat /etc/passwd); `id`; rm -rf / || echo pwned"
        cmd, args = self._inject("echo", [nasty, "a|b", "c&&d", "e;f"])
        assert cmd == "echo"
        assert args == [nasty, "a|b", "c&&d", "e;f"]

    def test_backticks_preserved(self) -> None:
        cmd, args = self._inject("echo", ["`whoami`", "$USER"])
        assert args == ["`whoami`", "$USER"]
        # The $ must NOT have been expanded by the runtime.
        assert "$USER" in args

    # --- 3. Path traversal ------------------------------------------------

    def test_path_traversal_preserved_literally(self) -> None:
        # The runtime must not resolve or reject `../` paths — the wrap
        # script + container boundary is what prevents escape.
        cmd, args = self._inject("cat", ["../../../etc/passwd", "/tmp/../etc/shadow"])
        assert cmd == "cat"
        assert args == ["../../../etc/passwd", "/tmp/../etc/shadow"]

    # --- 4. Shell globbing ------------------------------------------------

    def test_glob_characters_preserved(self) -> None:
        # ls *.txt must be passed literally — no expansion at injection time.
        cmd, args = self._inject("ls", ["*.txt", "?.log", "[abc]*"])
        assert cmd == "ls"
        assert args == ["*.txt", "?.log", "[abc]*"]

    # --- 5. Unicode paths -------------------------------------------------

    def test_unicode_cjk_preserved(self) -> None:
        path = "/projects/映画/シーン01/レンダー.exr"
        cmd, args = self._inject("render", ["--scene", path])
        assert cmd == "render"
        assert args == ["--scene", path]
        assert len(path.encode("utf-8")) > len(path)

    def test_unicode_emoji_preserved(self) -> None:
        cmd, args = self._inject("echo", ["🎬 render 🎥", "🔥"])
        assert args == ["🎬 render 🎥", "🔥"]

    def test_unicode_mixed_scripts_preserved(self) -> None:
        cmd, args = self._inject(
            "tool",
            ["--русский", "--中文", "--日本語", "--한국어", "--العربية"],
        )
        assert args == ["--русский", "--中文", "--日本語", "--한국어", "--العربية"]

    # --- 6. Empty and whitespace-only arguments ---------------------------

    def test_empty_string_arg_preserved(self) -> None:
        cmd, args = self._inject("echo", ["before", "", "after"])
        assert cmd == "echo"
        assert args == ["before", "", "after"]
        assert len(args) == 3

    def test_whitespace_only_arg_preserved(self) -> None:
        cmd, args = self._inject("echo", [" ", "  ", "   "])
        assert args == [" ", "  ", "   "]

    # --- 7. Newlines in arguments -----------------------------------------

    def test_newline_in_arg_preserved(self) -> None:
        # The EXPR extension relaxes the ArgString regex to accept
        # control characters; the runtime must preserve them literally.
        cmd, args = self._inject("echo", ["line1\nline2"])
        assert cmd == "echo"
        assert args == ["line1\nline2"]

    # --- 8. Near-limit command length -------------------------------------

    def test_large_argument_preserved(self) -> None:
        big = "x" * (100 * 1024)
        cmd, args = self._inject("cat", ["--data", big])
        assert cmd == "cat"
        assert args == ["--data", big]
        assert len(args[1]) == 100 * 1024

    def test_many_arguments_preserved(self) -> None:
        many = [f"arg{i}" for i in range(1000)]
        cmd, args = self._inject("tool", many)
        assert cmd == "tool"
        assert args == many
        assert len(args) == 1000


# ---------------------------------------------------------------------------
# Wrap-environment embedded-file reuse across tasks.
#
# The wrap environment's embedded-file PATHS are allocated once and reused
# for every task run through the wrap (so Env.File.* symbols stay stable and
# unnamed files do not accumulate on disk), while the file CONTENTS are
# re-resolved and rewritten per task (data may reference WrappedAction.*).
# ---------------------------------------------------------------------------


class TestWrapEnvEmbeddedFileReuse:
    def _wrap_env_with_unnamed_file(self) -> Environment_2023_09:
        from openjd.model.v2023_09 import (
            DataString as DataString_2023_09,
            EmbeddedFileText as EmbeddedFileText_2023_09,
            EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
        )

        # The embedded file is UNNAMED (no `filename`), so its on-disk path
        # is mkstemp-allocated; its data references WrappedAction.Command,
        # so contents must be rewritten per task.
        return Environment_2023_09(
            name="WrapEnv",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onWrapEnvEnter=_NOOP,
                    onWrapTaskRun=Action_2023_09(
                        command=CommandString_2023_09("cat"),
                        args=[ArgString_2023_09("{{ Env.File.WrapData }}")],
                    ),
                    onWrapEnvExit=_NOOP,
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="WrapData",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data=DataString_2023_09("wrapped-command={{WrappedAction.Command}}\n"),
                    )
                ],
            ),
        )

    def test_unnamed_wrap_file_path_reused_across_tasks(self) -> None:
        # GIVEN: a wrap environment with an unnamed embedded file whose data
        # references WrappedAction.Command, and three tasks with distinct
        # wrapped commands (never executed; the wrap action runs instead).
        env = self._wrap_env_with_unnamed_file()
        commands = ("cmd-one", "cmd-two", "cmd-three")
        file_paths = []

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            identifier = session.enter_environment(environment=env)
            _run_until_ready(session)
            assert session.state == SessionState.READY

            for command in commands:
                # WHEN: a task runs through the wrap.
                session.run_task(
                    step_script=_step_script(command, []),
                    task_parameter_values={},
                    step_name="Step",
                )
                _run_until_ready(session)

                # THEN: the task succeeded, and the files directory contains
                # exactly ONE file for the record (not one per task) ...
                assert session.state == SessionState.READY
                status = session.action_status
                assert status is not None
                assert status.state == ActionState.SUCCESS
                files = [p for p in session.files_directory.iterdir() if p.is_file()]
                assert len(files) == 1
                # ... whose contents reflect THIS task's wrapped command.
                assert files[0].read_text() == f"wrapped-command={command}\n"
                file_paths.append(files[0])

            # THEN: the file path is identical across all three tasks.
            assert file_paths[0] == file_paths[1] == file_paths[2]

            session.exit_environment(identifier=identifier)
            _run_until_ready(session)


# ---------------------------------------------------------------------------
# step_name is required once a wrap environment is active: RFC 0008's
# WrappedStep.Name has no value to render without it, and <StepName> has a
# minimum length of one, so there is no empty sentinel to fall back on.
# ---------------------------------------------------------------------------


class TestWrappedStepNameRequired:
    def test_run_task_without_step_name_raises(self) -> None:
        # GIVEN: a wrap environment is entered
        env = _wrap_env(
            "container",
            Action_2023_09(
                command=CommandString_2023_09("echo"),
                args=[ArgString_2023_09("{{WrappedStep.Name}}")],
            ),
        )
        step = _step_script("echo", ["hello"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            identifier = session.enter_environment(environment=env)
            _run_until_ready(session)

            # WHEN / THEN: omitting step_name is caller misuse, reported as such
            # rather than silently rendering an empty container name.
            with pytest.raises(ValueError, match="requires step_name"):
                session.run_task(step_script=step, task_parameter_values={})

            # AND: the session is left usable — nothing was started, so the
            # caller can retry with a step name.
            assert session.state == SessionState.READY
            session.run_task(step_script=step, task_parameter_values={}, step_name="RetriedStep")
            _run_until_ready(session)
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS

            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

    def test_run_task_without_step_name_is_fine_unwrapped(self) -> None:
        # GIVEN: no wrap environment
        step = _step_script("echo", ["hello"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            # WHEN: step_name is still optional on the unwrapped path
            session.run_task(step_script=step, task_parameter_values={})
            _run_until_ready(session)

            # THEN
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS


# ---------------------------------------------------------------------------
# A wrap hook resolves in the wrap environment's own scope, which in openjd-rs
# is that environment's frozen enter-time symbol table. So a step environment
# that defines wrap hooks must carry the step-level `let` values it was entered
# with -- which reach it in its resolved symbol table -- into every hook
# invocation, without letting them replace the wrapped action's own resolution
# scope.
# ---------------------------------------------------------------------------


class TestWrapHookSeesEnterTimeStepScope:
    def test_hook_resolves_step_level_let_bindings(self, tmp_path) -> None:
        # GIVEN: a wrap environment entered with a step's resolved table,
        # carrying a step-level `let` value, and a hook referencing it.
        env = _wrap_env(
            "WrapEnv",
            Action_2023_09(
                command=CommandString_2023_09("echo"),
                args=[ArgString_2023_09("HOOK-{{greeting}}")],
            ),
        )
        step = _step_script("echo", ["INNER"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            identifier = session.enter_environment(
                environment=env,
                resolved_symtab=_serialized_table(
                    [{"name": "greeting", "type": "string", "value": "from-step-let"}]
                ),
                step_name="Step1",
            )
            _run_until_ready(session)
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

            # WHEN
            session.run_task(step_script=step, task_parameter_values={}, step_name="Step1")
            _run_until_ready(session)

            # THEN: the hook resolved the step-level binding instead of failing
            # with "Undefined variable".
            status = session.action_status
            assert status is not None
            assert status.state == ActionState.SUCCESS

            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

    def test_wrap_env_step_name_does_not_reach_the_wrapped_action(self) -> None:
        """The wrap env's enter-time step context must not replace the running
        step's in the wrapped action's own scope.

        This goes through ``run_task`` on purpose: the ordering it pins lives in
        ``run_task`` (seed the hook's scope only *after* the wrapped action's own
        scope has been built), so a test that calls the two helpers itself in its
        own order would re-assert its own script instead.
        """
        # GIVEN: a wrap env entered for "StepA", and a step whose onRun echoes
        # {{Step.Name}} -- which must resolve to the step actually running.
        hook_args: list[str] = []

        env = _wrap_env(
            "WrapEnv",
            Action_2023_09(
                command=CommandString_2023_09("echo"),
                args=[ArgString_2023_09("{{Step.Name}}")],
            ),
        )
        step = _step_script("echo", ["{{Step.Name}}"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            identifier = session.enter_environment(environment=env, step_name="StepA")
            _run_until_ready(session)

            original = session._inject_wrapped_task_symbols

            def _capture(symtab, step_script, step_name, *, inner_symtab):
                original(symtab, step_script, step_name, inner_symtab=inner_symtab)
                hook_args.extend(symtab["WrappedAction.Args"])

            session._inject_wrapped_task_symbols = _capture  # type: ignore[method-assign]

            # WHEN: a task of a DIFFERENT step runs under that wrap env
            session.run_task(step_script=step, task_parameter_values={}, step_name="StepB")
            _run_until_ready(session)

            # THEN: the wrapped action resolved with the RUNNING step's name.
            # Seeding the hook's scope before injection instead would give StepA.
            assert hook_args == ["StepB"]
            assert session.action_status is not None
            assert session.action_status.state == ActionState.SUCCESS

            session.exit_environment(identifier=identifier)
            _run_until_ready(session)

    @pytest.mark.parametrize("hook_phase", ["enter", "exit"])
    def test_env_hooks_resolve_step_level_let_bindings(self, hook_phase: str) -> None:
        """The seeding applies to onWrapEnvEnter and onWrapEnvExit too, not just
        onWrapTaskRun."""
        # GIVEN: a wrap env entered with a step-level binding, and an inner env
        # whose onEnter/onExit the hooks intercept.
        wrap_action = Action_2023_09(
            command=CommandString_2023_09("echo"),
            args=[ArgString_2023_09("HOOK-{{greeting}}")],
        )
        env = Environment_2023_09(
            name="WrapEnv",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onWrapEnvEnter=wrap_action,
                    onWrapTaskRun=_NOOP,
                    onWrapEnvExit=wrap_action,
                ),
            ),
        )
        inner = Environment_2023_09(
            name="Inner",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=CommandString_2023_09("true")),
                    onExit=Action_2023_09(command=CommandString_2023_09("true")),
                ),
            ),
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            wrap_id = session.enter_environment(
                environment=env,
                resolved_symtab=_serialized_table(
                    [{"name": "greeting", "type": "string", "value": "from-step-let"}]
                ),
                step_name="Step1",
            )
            _run_until_ready(session)

            # WHEN
            inner_id = session.enter_environment(environment=inner)
            _run_until_ready(session)
            if hook_phase == "enter":
                # THEN: the onWrapEnvEnter hook resolved the binding
                status = session.action_status
                assert status is not None
                assert status.state == ActionState.SUCCESS
            else:
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)
                # THEN: the onWrapEnvExit hook resolved the binding
                status = session.action_status
                assert status is not None
                assert status.state == ActionState.SUCCESS

            if hook_phase == "enter":
                session.exit_environment(identifier=inner_id)
                _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)


# ---------------------------------------------------------------------------
# The session must never claim RUNNING while it has no runner. cancel_action()
# is a cross-thread API guarded only by `state == RUNNING`, so such a window is
# one in which a cancel is lost -- and the RFC 0008 branches do real work
# (materializing the inner entity's embedded files, evaluating its `let`
# bindings, allocating the hook's file records) before a runner exists.
# ---------------------------------------------------------------------------


class TestNoRunningWithoutRunner:
    def _wrap_env_all_hooks(self) -> Environment_2023_09:
        act = Action_2023_09(command=CommandString_2023_09("true"))
        return Environment_2023_09(
            name="WrapEnv",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onWrapEnvEnter=act, onWrapTaskRun=act, onWrapEnvExit=act
                ),
            ),
        )

    def _inner_env(self) -> Environment_2023_09:
        act = Action_2023_09(command=CommandString_2023_09("true"))
        return Environment_2023_09(
            name="Inner",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(onEnter=act, onExit=act),
            ),
        )

    def test_state_is_not_running_during_wrap_setup(self) -> None:
        """Observed at the last point before the runner is built, in all three
        wrap paths."""
        observed: list[tuple[str, SessionState]] = []

        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            original = session._get_wrap_env_file_records

            def _observe(wrap_env):  # type: ignore[no-untyped-def]
                # _get_wrap_env_file_records is the last step before the runner
                # is constructed, so this is the worst case for the window.
                observed.append((wrap_env.name, session.state))
                return original(wrap_env)

            session._get_wrap_env_file_records = _observe  # type: ignore[method-assign]

            wrap_id = session.enter_environment(environment=self._wrap_env_all_hooks())
            _run_until_ready(session)

            inner_id = session.enter_environment(environment=self._inner_env())
            _run_until_ready(session)

            session.run_task(
                step_script=_step_script("echo", ["hi"]),
                task_parameter_values={},
                step_name="Step1",
            )
            _run_until_ready(session)

            session.exit_environment(identifier=inner_id)
            _run_until_ready(session)
            session.exit_environment(identifier=wrap_id)
            _run_until_ready(session)

        # THEN: all three wrap paths were exercised, and none of them had
        # already flipped the session to RUNNING.
        assert len(observed) == 3, observed
        assert all(state is not SessionState.RUNNING for _, state in observed), observed
