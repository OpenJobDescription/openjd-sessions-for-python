# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the ``onWrapTaskRun`` environment action (RFC 0008).

These tests exercise the end-to-end behaviour that a job template's ``onRun`` is
intercepted and the active environment's ``onWrapTaskRun`` is executed in its
place, with ``WrappedAction.Command``, ``WrappedAction.Args``,
``WrappedAction.Environment``, ``WrappedAction.Timeout``, and
``WrappedStep.Name`` injected into the wrap action's symbol table.
"""

from __future__ import annotations

import time
import uuid

import pytest

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

    def test_injects_wrapped_environment_as_key_value_list(self) -> None:
        # RFC 0008 (openjd-rs #277): WrappedAction.Environment carries every
        # session-defined variable — openjd_env definitions (applied via
        # simplify_ordered_changes) AND the declarative variables:-map seed.
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            from openjd.sessions._session import (
                EnvironmentVariableSetChange,
                SimplifiedEnvironmentVariableChanges,
            )

            fake_id = "env-1"
            session._environments_entered.append(fake_id)
            changes = SimplifiedEnvironmentVariableChanges({"DECLARED": "from-variables-map"})
            changes.simplify_ordered_changes(
                [
                    EnvironmentVariableSetChange(name="FOO", value="bar"),
                    EnvironmentVariableSetChange(name="BAZ", value="qux"),
                ]
            )
            session._created_env_vars[fake_id] = changes

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
            session.run_task(step_script=step, task_parameter_values={})
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
            session.run_task(step_script=step, task_parameter_values={})
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

            session.run_task(step_script=step, task_parameter_values={})
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

            session.run_task(step_script=step, task_parameter_values={})
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

            session.run_task(step_script=_step_script("cmd-one", []), task_parameter_values={})
            _run_until_ready(session)
            session.run_task(step_script=_step_script("cmd-two", []), task_parameter_values={})
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
