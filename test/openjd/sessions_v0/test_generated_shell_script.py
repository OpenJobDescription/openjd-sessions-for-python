# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Quoting in the shell script generated for a POSIX action.

The script is ``exec``’d by ``/bin/sh``, so a single unquoted metacharacter
anywhere in it is arbitrary code execution as the session user.
"""

import sys
from datetime import timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Optional
from unittest.mock import MagicMock

import pytest


from openjd.sessions._os_checker import is_posix
from openjd.sessions._runner_base import POSIX_SHELL_NAME_RE, ScriptRunnerBase
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger


class _Runner(ScriptRunnerBase):
    """Minimal concrete runner: only _generate_command_shell_script is exercised."""

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        pass


@pytest.mark.skipif(not is_posix(), reason="the generated shell script is POSIX-only")
class TestGeneratedShellScriptQuoting:
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
