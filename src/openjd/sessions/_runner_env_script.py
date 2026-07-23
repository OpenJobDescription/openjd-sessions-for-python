# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from datetime import timedelta
from ._logging import LoggerAdapter
from pathlib import Path
from typing import Callable, Optional

from openjd.model import SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import EnvironmentScript as EnvironmentScript_2023_09
from ._embedded_files import EmbeddedFilesScope, _FileRecord
from ._logging import log_subsection_banner
from ._runner_base import (
    ScriptRunnerBase,
    ScriptRunnerState,
)
from ._session_user import SessionUser
from ._types import (
    ENV_ACTION_DEFAULT_NOTIFY_PERIOD_SECONDS,
    ActionModel,
    ActionState,
    EnvironmentScriptModel,
)

__all__ = ("EnvironmentScriptRunner", "WRAP_HOOK_ACTION_NAMES")


WRAP_HOOK_ACTION_NAMES = ("onWrapEnvEnter", "onWrapTaskRun", "onWrapEnvExit")
"""The three RFC 0008 wrap-hook action names an Environment's script may
define. Single-sourced here for the runner's hook dispatch and the Session's
wrap-environment lookup/validation."""


_ENV_EXIT_DEFAULT_TIMEOUT = timedelta(minutes=5)
"""The default timeout for environment exit actions if none is specified.

See https://github.com/OpenJobDescription/openjd-specifications/wiki/2023-09-Template-Schemas#5-action
"""


class EnvironmentScriptRunner(ScriptRunnerBase):
    """Use this to run actions from an Environment."""

    _environment_script: Optional[EnvironmentScriptModel]
    """The environment script that we're running.
    """

    _symtab: SymbolTable
    """Treat this as immutable.
    A SymbolTable containing values for all defined variables in the Step
    Script's scope (exluding any symbols defined within the Step Script itself).
    """

    _session_files_directory: Path
    """The location in the filesystem where embedded files will be materialized.
    """

    _action: Optional[ActionModel]
    """If defined, then this is the action that is currently running, or was last run.
    """

    _preallocated_file_records: Optional[list[_FileRecord]]
    """RFC 0008: the script's embedded-file records with on-disk paths already
    allocated by the Session (a wrap environment's files are allocated once
    and reused across wrap-hook invocations). When set, the runner skips path
    allocation and only re-resolves/rewrites the file contents.
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
        environment_script: Optional[EnvironmentScriptModel] = None,
        symtab: SymbolTable,
        # Directory within which files/attachments should be materialized
        session_files_directory: Path,
        # RFC 0008: pre-allocated embedded-file records to reuse (see
        # _preallocated_file_records)
        preallocated_file_records: Optional[list[_FileRecord]] = None,
    ):
        """
        Arguments (from base class):
            logger (Logger): The logger to which all messages should be sent from this and the
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
            environment (EnvironmentScriptModel): The Environment Script model that we're going to be running.
            symtab (SymbolTable): A SymbolTable containing values for all defined variables in the Step
                Script's scope (exluding any symbols defined within the Step Script itself).
            session_files_directory (Path): The location in the filesystem where embedded files will
                be materialized.
            preallocated_file_records (Optional[list[_FileRecord]]): RFC 0008: the script's
                embedded-file records with on-disk paths already allocated by the Session
                (a wrap environment's files are allocated once and reused across wrap-hook
                invocations). When given, the runner skips path allocation and only
                re-resolves/rewrites the file contents.
        """
        super().__init__(
            logger=logger,
            user=user,
            os_env_vars=os_env_vars,
            session_working_directory=session_working_directory,
            startup_directory=startup_directory,
            callback=callback,
        )
        self._environment_script = environment_script
        self._symtab = symtab
        self._session_files_directory = session_files_directory
        self._action = None
        self._preallocated_file_records = preallocated_file_records

        if self._environment_script and not isinstance(
            self._environment_script, EnvironmentScript_2023_09
        ):
            raise NotImplementedError("Unknown model type")

    def _run_env_action(
        self,
        action: ActionModel,
        *,
        default_timeout: Optional[timedelta] = None,
    ) -> None:
        """Run a specific given action from this Environment."""

        log_subsection_banner(self._logger, "Phase: Setup")

        let_bindings = (
            getattr(self._environment_script, "let", None)
            if self._environment_script is not None
            else None
        )
        # Write any embedded files to disk. File paths are allocated before
        # the script's EXPR `let` bindings evaluate (so bindings can reference
        # Env.File.*), and contents are written after (so `data` can reference
        # let-bound values) — mirroring the openjd-rs runner.
        if (
            self._environment_script is not None
            and self._environment_script.embeddedFiles is not None
        ):
            symtab = SymbolTable(source=self._symtab)
            # Note: _materialize_files calls the callback if it fails.
            self._materialize_files(
                EmbeddedFilesScope.ENV,
                self._environment_script.embeddedFiles,
                self._session_files_directory,
                symtab,
                let_bindings=let_bindings,
                preallocated_records=self._preallocated_file_records,
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
        self._action = action
        self._run_action(self._action, symtab, default_timeout=default_timeout)

    def enter(self) -> None:
        """Run the Environment's onEnter action."""
        if self.state != ScriptRunnerState.READY:
            raise RuntimeError("This cannot be used to run a second subprocess.")

        # For the type checker
        if self._environment_script is not None:
            assert isinstance(self._environment_script, EnvironmentScript_2023_09)
        if self._environment_script is None or self._environment_script.actions.onEnter is None:
            self._state_override = ScriptRunnerState.SUCCESS
            # Nothing to do, no action defined. Call the callback
            # to inform the caller that the run is complete, and then exit.
            if self._callback is not None:
                self._callback(ActionState.SUCCESS)
            return

        self._run_env_action(self._environment_script.actions.onEnter)

    def exit(self) -> None:
        """Run the Environment's onExit action."""
        if self.state != ScriptRunnerState.READY:
            raise RuntimeError("This cannot be used to run a second subprocess.")

        # For the type checker
        if self._environment_script is not None:
            assert isinstance(self._environment_script, EnvironmentScript_2023_09)
        if self._environment_script is None or self._environment_script.actions.onExit is None:
            self._state_override = ScriptRunnerState.SUCCESS
            # Nothing to do, no action defined. Call the callback
            # to inform the caller that the run is complete, and then exit.
            if self._callback is not None:
                self._callback(ActionState.SUCCESS)
            return

        self._run_env_action(
            self._environment_script.actions.onExit,
            default_timeout=_ENV_EXIT_DEFAULT_TIMEOUT,
        )

    def wrap_task_run(self) -> None:
        """Run the Environment's onWrapTaskRun action, wrapping a task's onRun."""
        self._run_wrap_hook("onWrapTaskRun")

    def wrap_env_enter(self) -> None:
        """RFC 0008: run this Environment's ``onWrapEnvEnter`` action,
        substituting it for an inner environment's ``onEnter``."""
        self._run_wrap_hook("onWrapEnvEnter")

    def wrap_env_exit(self) -> None:
        """RFC 0008: run this Environment's ``onWrapEnvExit`` action,
        substituting it for an inner environment's ``onExit``."""
        self._run_wrap_hook("onWrapEnvExit", default_timeout=_ENV_EXIT_DEFAULT_TIMEOUT)

    def _run_wrap_hook(self, hook: str, *, default_timeout: Optional[timedelta] = None) -> None:
        """Common dispatch for the three RFC 0008 wrap hooks. ``hook`` is
        one of ``onWrapEnvEnter``, ``onWrapTaskRun``, or ``onWrapEnvExit``."""
        if hook not in WRAP_HOOK_ACTION_NAMES:
            # Guard the getattr below: without this, a typo'd hook name
            # would silently become a SUCCESS no-op.
            raise ValueError(f"Unknown wrap hook name: {hook}")
        if self.state != ScriptRunnerState.READY:
            raise RuntimeError("This cannot be used to run a second subprocess.")

        # For the type checker
        if self._environment_script is not None:
            assert isinstance(self._environment_script, EnvironmentScript_2023_09)

        action = (
            getattr(self._environment_script.actions, hook, None)
            if self._environment_script is not None
            else None
        )
        if action is None:
            self._state_override = ScriptRunnerState.SUCCESS
            # Nothing to do, no wrap action defined. Call the callback
            # to inform the caller that the run is complete, and then exit.
            if self._callback is not None:
                self._callback(ActionState.SUCCESS)
            return

        if default_timeout is not None:
            self._run_env_action(action, default_timeout=default_timeout)
        else:
            self._run_env_action(action)

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        if self._action is None:
            # Nothing to do.
            return

        # For the type checker
        assert isinstance(self._action, Action_2023_09)

        self._cancel_with_effective_cancelation(
            cancelation=self._action.cancelation,
            symtab=self._symtab,
            default_notify_period_seconds=ENV_ACTION_DEFAULT_NOTIFY_PERIOD_SECONDS,
            time_limit=time_limit,
            mark_action_failed=mark_action_failed,
        )
