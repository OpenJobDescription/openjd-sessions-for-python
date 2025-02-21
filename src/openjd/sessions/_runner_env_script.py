# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from datetime import timedelta
from ._logging import LoggerAdapter
from pathlib import Path
from typing import Callable, Optional

from openjd.model import SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import CancelationMode as CancelationMode_2023_09
from openjd.model.v2023_09 import (
    CancelationMethodNotifyThenTerminate as CancelationMethodNotifyThenTerminate_2023_09,
)
from openjd.model.v2023_09 import EnvironmentScript as EnvironmentScript_2023_09
from ._embedded_files import EmbeddedFilesScope
from ._logging import log_subsection_banner
from ._runner_base import (
    CancelMethod,
    NotifyCancelMethod,
    ScriptRunnerBase,
    ScriptRunnerState,
    TerminateCancelMethod,
)
from ._session_user import SessionUser
from ._types import ActionModel, ActionState, EnvironmentScriptModel

__all__ = ("EnvironmentScriptRunner",)


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

        # Write any embedded files to disk
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
            )
            if self.state == ScriptRunnerState.FAILED:
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

    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        if self._action is None:
            # Nothing to do.
            return

        # For the type checker
        assert isinstance(self._action, Action_2023_09)

        method: CancelMethod
        if (
            self._action.cancelation is None
            or self._action.cancelation.mode == CancelationMode_2023_09.TERMINATE
        ):
            # Note: Default cancelation for a 2023-09 Step Script is Terminate
            method = TerminateCancelMethod()
        else:
            model_cancel_method = self._action.cancelation
            # For the type checker
            assert isinstance(model_cancel_method, CancelationMethodNotifyThenTerminate_2023_09)
            if model_cancel_method.notifyPeriodInSeconds is None:
                # Default grace period is 30s for a 2023-09 Environment Script's notify cancel
                method = NotifyCancelMethod(terminate_delay=timedelta(seconds=30))
            else:
                method = NotifyCancelMethod(
                    terminate_delay=timedelta(seconds=model_cancel_method.notifyPeriodInSeconds)
                )

        # Note: If the given time_limit is less than that in the method, then the time_limit will be what's used.
        self._cancel(method, time_limit, mark_action_failed)
