# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from datetime import timedelta
from ._logging import LoggerAdapter
from pathlib import Path
from typing import Callable, Optional

from openjd.model import SymbolTable
from openjd.model.v2023_09 import StepScript as StepScript_2023_09
from ._embedded_files import EmbeddedFilesScope
from ._logging import log_subsection_banner
from ._runner_base import (
    ScriptRunnerBase,
    ScriptRunnerState,
)
from ._session_user import SessionUser
from ._types import TASK_RUN_DEFAULT_NOTIFY_PERIOD_SECONDS, ActionState, StepScriptModel

__all__ = ("StepScriptRunner",)


class StepScriptRunner(ScriptRunnerBase):
    """Use this to run actions from a Step Script."""

    _script: StepScriptModel
    """The step script that we're running.
    """

    _symtab: SymbolTable
    """Treat this as immutable.
    A SymbolTable containing values for all defined variables in the Step
    Script's scope (exluding any symbols defined within the Step Script itself).
    """

    _session_files_directory: Path
    """The location in the filesystem where embedded files will be materialized.
    """

    def __init__(
        self,
        *,
        logger: LoggerAdapter,
        user: Optional[SessionUser] = None,
        # environment for the subprocess that is run
        os_env_vars: Optional[dict[str, Optional[str]]] = None,
        # The working directory of the session
        session_working_directory: Path,
        # `cwd` for the subprocess that's run
        startup_directory: Optional[Path] = None,
        # Callback to invoke when a running action exits
        callback: Optional[Callable[[ActionState], None]] = None,
        script: StepScriptModel,
        symtab: SymbolTable,
        # Directory within which files/attachments should be materialized
        session_files_directory: Path,
    ):
        """
        Arguments (from base class):
            logger (LoggerAdapter): The logger to which all messages should be sent from this and the
                subprocess.
            os_env_vars (dict[str, str]): Environment variables and their values to inject into the
                running subprocess.
            session_working_directory (Path): The temporary directory in which the Session is running.
            user (Optional[SessionUser]): The user to run the subprocess as, if given. Defaults to the
                current user.
            startup_directory (Optional[Path]): cwd to set for the subprocess, if it's possible to set it.
            callback (Optional[Callable[[ActionState], None]]): Callback to invoke when the running
                subprocess has started,  exited, or failed to start. Defaults to None.
        Arguments (unique to this class):
            script (StepScriptModel): The Step Script model that we're going to be running.
            symtab (SymbolTable): A SymbolTable containing values for all defined variables in the Step
                Script's scope (exluding any symbols defined within the Step Script itself).
            session_files_directory (Path): The location in the filesystem where embedded files will
                be materialized.
        """
        super().__init__(
            logger=logger,
            user=user,
            os_env_vars=os_env_vars,
            session_working_directory=session_working_directory,
            startup_directory=startup_directory,
            callback=callback,
        )
        self._script = script
        self._symtab = symtab
        self._session_files_directory = session_files_directory

        if not isinstance(self._script, StepScript_2023_09):
            raise NotImplementedError("Unknown model type")

    def run(self) -> None:
        """Run the Step Script's onRun Action."""
        if self.state != ScriptRunnerState.READY:
            raise RuntimeError("This cannot be used to run a second subprocess.")

        log_subsection_banner(self._logger, "Phase: Setup")

        # For the type checker.
        assert isinstance(self._script, StepScript_2023_09)
        let_bindings = getattr(self._script, "let", None)
        # Write any embedded files to disk. File paths are allocated before
        # the script's EXPR `let` bindings evaluate (so bindings can reference
        # Task.File.*), and contents are written after (so `data` can
        # reference let-bound values) — mirroring the openjd-rs runner.
        if self._script.embeddedFiles is not None:
            symtab = SymbolTable(source=self._symtab)
            self._materialize_files(
                EmbeddedFilesScope.STEP,
                self._script.embeddedFiles,
                self._session_files_directory,
                symtab,
                let_bindings=let_bindings,
            )
            if self.state == ScriptRunnerState.FAILED:
                return
        elif let_bindings:
            symtab = SymbolTable(source=self._symtab)
            if not self._apply_let_bindings_or_fail(symtab, let_bindings):
                return
        else:
            symtab = self._symtab

        # Construct the command by evalutating the format strings in the command
        self._run_action(self._script.actions.onRun, symtab)

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        # For the type checker.
        assert isinstance(self._script, StepScript_2023_09)

        self._cancel_with_effective_cancelation(
            cancelation=self._script.actions.onRun.cancelation,
            symtab=self._symtab,
            default_notify_period_seconds=TASK_RUN_DEFAULT_NOTIFY_PERIOD_SECONDS,
            time_limit=time_limit,
            mark_action_failed=mark_action_failed,
        )
