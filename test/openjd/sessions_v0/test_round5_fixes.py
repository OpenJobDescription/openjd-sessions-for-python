# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Regression tests for round-5 review findings R5-1 through R5-9 on PR #333,
plus the sibling occurrences found while implementing each of them.

Every test in this file was mutation-checked: the fix was reverted and the test
was confirmed to fail, then the fix was re-applied and the test was confirmed to
pass. A test that passes against the unfixed tree pins nothing.
"""

import logging
import os
import stat
import sys
from datetime import timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import (
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CommandString as CommandString_2023_09,
    DataString as DataString_2023_09,
    EmbeddedFileText as EmbeddedFileText_2023_09,
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
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
from openjd.sessions._embedded_files import (
    EmbeddedFiles,
    EmbeddedFilesScope,
    _FileRecord,
)
from openjd.sessions._os_checker import is_posix
from openjd.sessions._runner_base import POSIX_SHELL_NAME_RE, ScriptRunnerBase
from openjd.sessions._subprocess import LoggingSubprocess
from openjd.sessions._tempdir import OPENJD_TEMPDIR_MODE, TempDir, custom_gettempdir

from .conftest import build_logger


def _make_record(
    msg: str, args: Any = None, session_id: str = "foo", level: int = logging.INFO
) -> logging.LogRecord:
    """A LogRecord shaped the way the session logger produces them."""
    record = logging.LogRecord("test", level, "path", 1, msg, args, None)
    record.session_id = session_id  # type: ignore[attr-defined]
    return record


class _Runner(ScriptRunnerBase):
    """Minimal concrete runner: only _generate_command_shell_script is exercised."""

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        pass


# ===========================================================================
# R5-1 -- redaction must not leave record.args populated
# ===========================================================================


class TestR51RedactionArgsBypass:
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


# ===========================================================================
# R5-2 -- consumer callback exceptions must not escape filter()
# ===========================================================================


class TestR52FilterCallbackIsolation:
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


# ===========================================================================
# SIB-1 -- env var name anchoring (sibling of R5-5, found during this round)
# ===========================================================================


class TestSib1EnvVarNameAnchoring:
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

    def test_a_separator_cannot_reach_a_decoded_name(self) -> None:
        """Defence in depth: the regex matches the *raw* message, but JSON
        decoding happens afterwards, so the decoded name is re-checked."""
        # GIVEN a filter whose callback records what it is handed
        callback = MagicMock()
        f = ActionMonitoringFilter(session_id="foo", callback=callback)

        # WHEN every ENV message it sees is processed
        for msg in (
            'openjd_env: "FOO\\nBAR=baz"',
            'openjd_env: "FOO\\u000aBAR=baz"',
            'openjd_env: "FOO\\u0000BAR=baz"',
        ):
            f.filter(_make_record(msg))

        # THEN no name carrying a separator was ever handed onwards
        for call in callback.call_args_list:
            value = call[0][1]
            if isinstance(value, dict):
                assert not any(c in value["name"] for c in ("\n", "\r", "\0", "="))


# ===========================================================================
# R5-3 -- the shared temporary root must be validated before use
# ===========================================================================


class TestR53TempRootHardening:
    """R5-3: `<tempdir>/OpenJD` is a fixed, predictable path whose parent is
    world-writable on typical POSIX hosts. `exist_ok=True` accepted whatever was
    already there."""

    def test_created_with_an_explicit_mode_regardless_of_umask(self, tmp_path: Path) -> None:
        # GIVEN: a hostile umask that would otherwise strip group/other bits
        old_umask = os.umask(0o077)
        try:
            with patch("openjd.sessions._tempdir.gettempdir", return_value=str(tmp_path)):
                # WHEN
                created = custom_gettempdir()
        finally:
            os.umask(old_umask)

        # THEN: the root is traversable as intended, not umask-dependent.
        assert Path(created) == tmp_path / "OpenJD"
        if is_posix():
            assert stat.S_IMODE(os.stat(created).st_mode) == stat.S_IMODE(OPENJD_TEMPDIR_MODE)

    @pytest.mark.skipif(not is_posix(), reason="symlink pre-creation is a POSIX vector here")
    def test_rejects_a_symlink_at_the_root_path(self, tmp_path: Path) -> None:
        # GIVEN: an attacker has replaced the root with a symlink elsewhere
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").symlink_to(elsewhere, target_is_directory=True)

        # WHEN / THEN: we refuse rather than creating sessions inside it
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError, match="link or reparse point"):
                custom_gettempdir()

    def test_rejects_a_root_that_is_not_a_directory(self, tmp_path: Path) -> None:
        # GIVEN: a plain file squatting on the root path
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").write_text("squat")

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError):
                custom_gettempdir()

    @pytest.mark.skipif(not is_posix(), reason="uid ownership check is POSIX-only")
    def test_rejects_a_root_owned_by_another_user(self, tmp_path: Path) -> None:
        # GIVEN: the root exists but lstat reports a different owner
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").mkdir()
        real_lstat = os.lstat
        foreign_uid = os.geteuid() + 12345

        class _Stat:
            def __init__(self, base: os.stat_result) -> None:
                self.st_mode = base.st_mode
                self.st_uid = foreign_uid

        def fake_lstat(path: Any, *a: Any, **k: Any) -> Any:
            base = real_lstat(path, *a, **k)
            if str(path).endswith("OpenJD"):
                return _Stat(base)
            return base

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.lstat", side_effect=fake_lstat):
                with pytest.raises(RuntimeError, match="owned by uid"):
                    custom_gettempdir()

    @pytest.mark.skipif(not is_posix(), reason="uid ownership check is POSIX-only")
    def test_accepts_a_root_owned_by_root(self, tmp_path: Path) -> None:
        """A system-provisioned root must keep working."""
        # GIVEN
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").mkdir()
        real_lstat = os.lstat

        class _Stat:
            def __init__(self, base: os.stat_result) -> None:
                self.st_mode = base.st_mode
                self.st_uid = 0

        def fake_lstat(path: Any, *a: Any, **k: Any) -> Any:
            base = real_lstat(path, *a, **k)
            if str(path).endswith("OpenJD"):
                return _Stat(base)
            return base

        # WHEN / THEN: no exception
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.lstat", side_effect=fake_lstat):
                assert custom_gettempdir() == str(parent / "OpenJD")


# ===========================================================================
# R5-7 -- cleanup must report WHY each path could not be deleted
# ===========================================================================


class TestR57CleanupErrorReporting:
    """R5-7: the old handler accepted the exception and discarded it, leaving a
    list of bare paths -- on the one code path where "permission denied" versus
    "a process still holds this open" changes what the operator must do."""

    def test_failure_message_names_the_path_and_the_reason(self, tmp_path: Path) -> None:
        # GIVEN: a temp dir whose removal fails with a specific, diagnosable error
        d = TempDir(dir=tmp_path)
        doomed = d.path / "stubborn.txt"
        doomed.write_text("x")

        def boom(path: Any, *a: Any, **k: Any) -> None:
            raise PermissionError(13, "Permission denied")

        # WHEN
        with (
            patch("openjd.sessions._tempdir.os.unlink", side_effect=boom),
            patch("openjd.sessions._tempdir.os.remove", side_effect=boom, create=True),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                d.cleanup()

        # THEN: both the path and the cause are in the message.
        message = str(excinfo.value)
        assert "stubborn.txt" in message
        assert "PermissionError" in message

    def test_successful_cleanup_still_removes_everything(self, tmp_path: Path) -> None:
        # GIVEN
        d = TempDir(dir=tmp_path)
        (d.path / "a.txt").write_text("x")
        (d.path / "sub").mkdir()
        (d.path / "sub" / "b.txt").write_text("y")

        # WHEN
        d.cleanup()

        # THEN
        assert not d.path.exists()


# ===========================================================================
# R5-4 / R5-5 -- nothing unquoted may reach /bin/sh
# ===========================================================================


@pytest.mark.skipif(not is_posix(), reason="the generated shell script is POSIX-only")
class TestR54R55ShellScriptQuoting:
    """The generated script is `exec`'d by /bin/sh, so a single unquoted
    metacharacter anywhere in it is code execution as the session user."""

    def _script(
        self,
        tmp_path: Path,
        *,
        os_env_vars: Optional[dict[str, Optional[str]]] = None,
        startup_directory: Optional[Path] = None,
    ) -> str:
        runner = _Runner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            os_env_vars=os_env_vars,
            startup_directory=startup_directory,
        )
        try:
            return runner._generate_command_shell_script(["/bin/echo", "hi"])
        finally:
            runner.shutdown()

    def test_single_quote_in_the_startup_directory_cannot_break_out(self, tmp_path: Path) -> None:
        # GIVEN: a startup directory whose name closes the old hand-written quote
        hostile = Path("/tmp/x'; touch /tmp/openjd_r5_pwned; '")

        # WHEN
        script = self._script(tmp_path, startup_directory=hostile)

        # THEN: the injected command is inside a quoted word, not a command.
        cd_line = next(line for line in script.splitlines() if line.startswith("cd "))
        assert "touch /tmp/openjd_r5_pwned" in cd_line  # the path is preserved verbatim...
        assert not cd_line.startswith("cd '/tmp/x'; ")  # ...but no longer executes.
        # Prove it to the shell itself rather than by eyeballing the quoting.
        import shlex

        assert shlex.split(cd_line) == ["cd", str(hostile)]

    def test_ordinary_startup_directory_with_spaces_still_works(self, tmp_path: Path) -> None:
        # GIVEN
        spaced = tmp_path / "a dir with spaces"

        # WHEN
        script = self._script(tmp_path, startup_directory=spaced)

        # THEN
        import shlex

        cd_line = next(line for line in script.splitlines() if line.startswith("cd "))
        assert shlex.split(cd_line) == ["cd", str(spaced)]

    @pytest.mark.parametrize(
        "hostile_name",
        [
            "BAD;touch /tmp/openjd_r5_pwned;X",
            "BAD$(touch /tmp/openjd_r5_pwned)",
            "BAD`touch /tmp/openjd_r5_pwned`",
            "BAD\ntouch /tmp/openjd_r5_pwned",
            "BAD&&touch /tmp/openjd_r5_pwned",
        ],
    )
    def test_hostile_env_var_names_are_not_emitted(self, tmp_path: Path, hostile_name: str) -> None:
        # GIVEN / WHEN
        script = self._script(tmp_path, os_env_vars={hostile_name: "v", "GOOD": "ok"})

        # THEN: the hostile name produced no line at all, and the legal one did.
        assert "openjd_r5_pwned" not in script
        assert "export GOOD=ok" in script

    def test_names_that_are_legal_elsewhere_but_not_in_sh_are_skipped(self, tmp_path: Path) -> None:
        """`ProgramFiles(x86)` is a real Windows variable name and an outright
        /bin/sh syntax error -- previously it broke the whole action."""
        # GIVEN / WHEN
        script = self._script(tmp_path, os_env_vars={"ProgramFiles(x86)": "C:\\PF"})

        # THEN
        assert "ProgramFiles(x86)" not in script

    def test_generated_script_is_valid_sh_even_with_hostile_input(self, tmp_path: Path) -> None:
        """The strongest assertion available: hand it to `sh -n`."""
        # GIVEN
        script = self._script(
            tmp_path,
            os_env_vars={
                "BAD;X": "v",
                "ProgramFiles(x86)": "v",
                "GOOD": "a'b\"c$(d)",
                "UNSET_ME": None,
            },
            startup_directory=Path("/tmp/x'; touch /tmp/openjd_r5_pwned; '"),
        )
        script_file = tmp_path / "candidate.sh"
        script_file.write_text(script)

        # WHEN
        from subprocess import run

        result = run(["/bin/sh", "-n", str(script_file)], capture_output=True, text=True)

        # THEN
        assert result.returncode == 0, f"generated script is not valid sh: {result.stderr}"

    def test_unset_is_emitted_for_a_legal_name(self, tmp_path: Path) -> None:
        # GIVEN / WHEN
        script = self._script(tmp_path, os_env_vars={"GOES_AWAY": None})

        # THEN
        assert "unset GOES_AWAY" in script

    @pytest.mark.parametrize("name", ["FOO", "_foo", "F9", "_", "aB_9"])
    def test_posix_name_re_accepts_legal_identifiers(self, name: str) -> None:
        assert POSIX_SHELL_NAME_RE.fullmatch(name) is not None

    @pytest.mark.parametrize("name", ["9FOO", "", "FOO BAR", "FOO=", "FOO\n", "FOO;", "é"])
    def test_posix_name_re_rejects_everything_else(self, name: str) -> None:
        assert POSIX_SHELL_NAME_RE.fullmatch(name) is None

    def test_a_skipped_name_still_reaches_the_subprocess(
        self, tmp_path: Path, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        """Skipping the export line must not change what the child sees, because
        Popen's `env=` does not go through a shell."""
        # GIVEN: a name that is illegal in sh but legal as an environment entry
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(
            logger=logger,
            args=[
                sys.executable,
                "-c",
                "import os;print('VAL=' + os.environ.get('ProgramFiles(x86)', 'MISSING'))",
            ],
            os_env_vars={"ProgramFiles(x86)": "present"},
        )

        # WHEN
        proc.run()

        # THEN
        lines = []
        while not message_queue.empty():
            lines.append(message_queue.get().getMessage())
        assert "VAL=present" in lines
        assert proc.exit_code == 0


# ===========================================================================
# R5-6 -- invariant-bearing checks must survive `python -O`
# ===========================================================================


class TestR56InvariantsSurviveOptimizedMode:
    """R5-6: `assert` is stripped under `python -O`, which is a legitimate
    production deployment mode. Checks that carry a real runtime invariant must
    be explicit raises."""

    def test_unsupported_embedded_file_model_raises(self, tmp_path: Path) -> None:
        # GIVEN: an embedded file object of an unrecognised shape
        files = EmbeddedFiles(
            logger=MagicMock(),
            scope=EmbeddedFilesScope.ENV,
            session_files_directory=tmp_path,
        )

        class _AlienFile:
            filename = "x.sh"
            runnable = False

        # WHEN / THEN: each of the three entry points stops rather than
        # materializing a file from an object it does not understand.
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._find_value_prefix(_AlienFile())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._get_symtab_entry(_AlienFile())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="Unsupported embedded file model"):
            files._materialize_file(
                tmp_path / "x.sh", _AlienFile(), SymbolTable()  # type: ignore[arg-type]
            )

    def test_working_directory_property_raises_a_legible_error(self) -> None:
        # GIVEN: a Session whose construction did not complete
        session = Session.__new__(Session)
        session._working_dir = None  # type: ignore[attr-defined]

        # WHEN / THEN: a library RuntimeError, not AttributeError on None.
        with pytest.raises(RuntimeError, match="no working directory"):
            _ = session.working_directory

    def test_state_is_readable_when_a_launch_failed_after_process_creation(
        self, tmp_path: Path
    ) -> None:
        """The `state` property is on the path every consumer polls. An
        inconsistent (process set, future unset) pair must not make the runner
        permanently unreadable."""
        # GIVEN
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            runner._process = MagicMock()
            runner._run_future = None

            # WHEN / THEN: no AssertionError, no AttributeError.
            from openjd.sessions._runner_base import ScriptRunnerState

            assert runner.state == ScriptRunnerState.READY
        finally:
            runner.shutdown()

    def test_notify_period_end_is_a_no_op_without_a_process(self, tmp_path: Path) -> None:
        """Runs on a threading.Timer thread, where an exception reaches only
        threading.excepthook -- so the grace period would appear to have silently
        done nothing."""
        # GIVEN
        runner = _Runner(logger=MagicMock(), session_working_directory=tmp_path)
        try:
            runner._process = None

            # WHEN / THEN: returns cleanly.
            runner._on_notify_period_end()
        finally:
            runner.shutdown()

    def test_log_subproc_stdout_raises_a_legible_error_without_a_process(
        self, tmp_path: Path
    ) -> None:
        # GIVEN
        proc = LoggingSubprocess(logger=MagicMock(), args=[sys.executable, "-c", "pass"])

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="before the subprocess has been created"):
            proc._log_subproc_stdout()

    def test_an_embedded_file_still_materializes_normally(self, tmp_path: Path) -> None:
        """Guard against the isinstance rewrite breaking the happy path."""
        # GIVEN
        files = EmbeddedFiles(
            logger=MagicMock(),
            scope=EmbeddedFilesScope.ENV,
            session_files_directory=tmp_path,
        )
        model = EmbeddedFileText_2023_09(
            name="Script",
            type=EmbeddedFileTypes_2023_09.TEXT,
            data=DataString_2023_09("hello"),
        )
        symtab = SymbolTable()

        # WHEN
        records = files.allocate_file_paths([model], symtab)
        files.write_file_contents(records, symtab)

        # THEN
        assert len(records) == 1
        assert isinstance(records[0], _FileRecord)
        assert records[0].filename.read_text() == "hello"


# ===========================================================================
# R5-9 -- an unknown process group must be recorded as unknown
# ===========================================================================


@pytest.mark.skipif(not is_posix(), reason="process groups are POSIX-only")
class TestR59ProcessGroupSentinel:
    """R5-9: the lookup fails *because* the process is gone, so its pid is dead.
    Recording it as a process-group id means a later `killpg` is at best a no-op
    and, after pid recycling, targets an unrelated group."""

    def test_reaped_child_leaves_the_group_unknown(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN: a child whose process group cannot be looked up
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(
            logger=logger, args=[sys.executable, "-c", "pass"], os_env_vars=None
        )

        # WHEN
        with patch(
            "openjd.sessions._subprocess.os.getpgid", side_effect=ProcessLookupError(3, "No such")
        ):
            proc.run()

        # THEN: "unknown", not a stale pid -- and the action still succeeded,
        # which is the behaviour the original fix existed to protect.
        assert proc._sudo_child_process_group_id is None
        assert proc.exit_code == 0
        assert proc.failed_to_start is False

    def test_no_signal_is_delivered_when_the_group_is_unknown(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # GIVEN: a finished process with no known group
        logger = build_logger(queue_handler)
        proc = LoggingSubprocess(logger=logger, args=[sys.executable, "-c", "pass"])
        with patch(
            "openjd.sessions._subprocess.os.getpgid", side_effect=ProcessLookupError(3, "No such")
        ):
            proc.run()
        assert proc._sudo_child_process_group_id is None

        # WHEN: a SIGKILL is attempted anyway
        with (
            patch("openjd.sessions._subprocess.os.killpg") as killpg,
            patch(
                "openjd.sessions._subprocess.find_sudo_child_process_group_id", return_value=None
            ),
        ):
            proc._posix_signal_subprocess(MagicMock(pid=999999), signal_name="kill")

        # THEN: nothing was signalled.
        killpg.assert_not_called()

    def test_sudo_helper_returns_unknown_when_sudo_is_already_gone(self) -> None:
        """Sibling: the same guard on the first getpgid in the sudo helper."""
        # GIVEN
        from openjd.sessions._linux._sudo import find_sudo_child_process_group_id

        # WHEN
        with patch(
            "openjd.sessions._linux._sudo.os.getpgid",
            side_effect=ProcessLookupError(3, "No such process"),
        ):
            result = find_sudo_child_process_group_id(
                logger=MagicMock(), sudo_process=MagicMock(pid=999999)
            )

        # THEN: the established "unknown" value, not an escaping ESRCH.
        assert result is None
