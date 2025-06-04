# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from logging import Filter
from os import name as os_name
from os import stat as os_stat
from pathlib import Path
from tempfile import mkstemp
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Optional, Type, Union

from openjd.model import (
    JobParameterValues,
    ParameterValue,
    ParameterValueType,
    RevisionExtensions,
    SpecificationRevision,
    SymbolTable,
    TaskParameterSet,
)
from openjd.model import version as model_version
from openjd.model.v2023_09 import (
    ValueReferenceConstants as ValueReferenceConstants_2023_09,
)
from ._action_filter import ActionMessageKind, ActionMonitoringFilter
from ._embedded_files import write_file_for_user
from ._logging import LOG, log_section_banner, LoggerAdapter, LogExtraInfo, LogContent
from ._os_checker import is_posix, is_windows
from ._path_mapping import PathMappingRule
from ._runner_base import ScriptRunnerBase
from ._runner_env_script import EnvironmentScriptRunner
from ._runner_step_script import StepScriptRunner
from ._session_user import SessionUser
from ._subprocess import LoggingSubprocess
from ._tempdir import TempDir, custom_gettempdir
from ._types import (
    ActionState,
    EnvironmentIdentifier,
    EnvironmentModel,
    StepScriptModel,
)
from ._version import version

if is_windows():  # pragma: nocover
    from subprocess import HIGH_PRIORITY_CLASS  # type: ignore

if TYPE_CHECKING:
    from openjd.model.v2023_09._model import EnvironmentVariableObject

__all__ = ("SessionState", "Session", "EnvironmentIdentifier")


class SessionState(str, Enum):
    READY = "ready"
    """The state of a Session when it is ready to run actions.
    """

    RUNNING = "running"
    """The state of a Session while it is actively running an action.
    """

    CANCELING = "canceling"
    """The state of a Session that is in the process of canceling the currently
    running action.
    """

    READY_ENDING = "ready_ending"
    """The state of a Session when it is ready to run only Environment End actions.
    The Session has previously experienced an error or cancelation running
    one of its actions, and is now only allowed to run Environment End actions to
    clean up the Session context."""

    ENDED = "ended"
    """Terminal state of a Session that has ended and can no longer run any actions."""


@dataclass(frozen=True)
class ActionStatus:
    state: ActionState
    """The runtime state of the action."""

    progress: Optional[float] = None
    """The progress of the action as reported by an "openjd_progress:" message"""

    status_message: Optional[str] = None
    """The status message for the action as reported by an "openjd_status:" message"""

    fail_message: Optional[str] = None
    """The failure reason of the action as reported by an "openjd_fail:" message."""

    exit_code: Optional[int] = None
    """The exit code of the action's process, if it has exited.
    Note: This may be None in SUCCESS & FAILED states.
    e.g.
        SUCCESS - Entered an environment that ran no Action to enter it.
        FAILED - Failed before trying to run the Action subprocess, such as
            failing to write embedded files to disk.
    """


@dataclass
class EnvironmentVariableSetChange:
    name: str
    value: str


@dataclass
class EnvironmentVariableUnsetChange:
    name: str


EnvironmentVariableChange = Union[EnvironmentVariableSetChange, EnvironmentVariableUnsetChange]


class SimplifiedEnvironmentVariableChanges:
    """
    Keeps track of what variables need to be set and unset for an environment. On Windows all environment keys
    are stored as uppercase. This is because while Windows environment variables are case insensitive, the win32 api
    allows you to store multiple keys of mixed case, for example the win32 api would allow you
    to set both "PATH" and "Path" as environment variable keys. This leads to undefined behaviour when calling one of the keys.
    """

    def __init__(self, initial_variables: Union[dict[str, str], "EnvironmentVariableObject"]):
        self._to_set: dict[str, Optional[str]]

        if is_windows():
            self._to_set = {}
            for var_name, var_value in initial_variables.items():
                self._to_set[var_name.upper()] = var_value
        else:
            self._to_set = dict(initial_variables)  # Make a copy

    def simplify_ordered_changes(self, changes: list[EnvironmentVariableChange]) -> None:
        """Apply a given list of sets and unsets to the current state in order"""
        for change in changes:
            if isinstance(change, EnvironmentVariableSetChange):
                self._to_set[change.name.upper() if is_windows() else change.name] = change.value
            elif isinstance(change, EnvironmentVariableUnsetChange):
                self._to_set[change.name.upper() if is_windows() else change.name] = None
            else:
                raise ValueError("Unknown type of environment variable change.")

    def apply_to_environment(self, env_vars: dict[str, Optional[str]]) -> None:
        """Modify a given dictionary of environment variables to reflect the changes"""
        if is_windows():
            # Environment variables on Windows are case insensitive when used, but are case sensitive when
            # set via the Windows API.
            for var_name, var_value in env_vars.copy().items():
                del env_vars[var_name]
                env_vars[var_name.upper()] = var_value

        # Note: An env var value of None means to unset that variable
        env_vars.update(self._to_set)


SessionCallbackType = Callable[[str, ActionStatus], None]


class Session(object):
    """A context for running actions of an Open Job Description Job.

    In Open Job Description, the Tasks for a Job's Steps are run within the context of a *Session*.
    Each Step in a Job defines the properties of the Session that are required to
    run its Tasks. Open Job Description sessions enable users to amortize expensive or time-consuming
    setup and tear-down operations in the worker's environment before and after a sequence
    of Tasks.

    A Session starts 0 or more *Environments* in the order given on the worker when
    it is started, and ends those Environments in reverse order when the Session is
    no longer needed. Each Environment defines a start and end *Action* — a command/script
    defined by the end-user — that is run on the worker when starting or ending the
    Environment.  The actions for these environments, as with all actions, are each
    run in their own operating system process.

    All stdout and stderr from the subproceseses run within this Session, and any additional
    logging generated by the Session itself, are forwarded to the Open Job Description sessions
    module's LOG Logger at log level INFO. The LogRecords sent to the log have an
    extra attribute named "session_id" whose value is the session_id that was passed
    to the constructor of the Session.

    Each Session has its own temporary working directory, referred to as the Session
    Working Directory. Instantiating this class creates this directory for the duration
    of the Session, and you are expected to call the cleanup() method on your Session
    to delete that directory when done (this is done automatically if using the Session
    as a context manager).
    On POSIX:
    - The Session Working Directory's owner is the process owner.
    - If a PosixSessionUser is provided, then the group of that user is the group owner
      of the the directory.
    On Windows:
    - All files and directories within the Session Working Directory inherit the
      directory's ACL.
    - The Session Working Directory's ACL is set so that the process owner has full
      control
    - If a WindowsSessionUser is provided then the user within that SessionUser
      is also given Modify access.
    """

    _state: SessionState

    _session_id: str
    """The application-provided id for this Session.
    """

    _logger: LoggerAdapter
    """The logger that all of this Session's running processes will send their logs to.
    """

    _ending_only: bool
    """The Session has previously experienced an error or cancelation of a running
    action, and can only run Environment-end actions.
    """

    _environments: dict[
        EnvironmentIdentifier,
        EnvironmentModel,
    ]
    """A mapping of identifier to Environment for each Environment entered in the session.
    """

    _environments_entered: list[EnvironmentIdentifier]
    """A list of the Environments entered (either successfully or unsuccessfully[failed/canceled]),
    in the order that they were entered.
    Environments must be exited in the reverse order to that which they were entered.
    """

    _runner: Optional[ScriptRunnerBase]
    """The currently running runner, if there is one.
    """

    _running_environment_identifier: Optional[str]
    """If we're running an environment action then this will be the
    identifier of that environment; otherwise it will be None.
    """

    _process_env: dict[str, str]
    """Mapping of environment variable names to values. Used as the shell/os environment
    when running a subprocess.
    """

    _created_env_vars: dict[EnvironmentIdentifier, SimplifiedEnvironmentVariableChanges]
    """OS environment variables defined by Open Job Description Environments
    """

    _log_filter: Filter
    """The handler that we've hooked to the LOG. Removed when the Session is deleted.
    """

    _working_dir: Optional[TempDir] = None
    """The Session Working Directory.
    """

    _files_dir: TempDir
    """The subdirectory of the Working Directory where embedded files
    are materialized.
    """

    _retain_working_dir: bool
    """If True, then the working directory is not deleted on cleanup.
    """

    _user: Optional[SessionUser]
    """The specific OS user to run subprocesses as, and whom will have permissions
    to the Session's Working Directory.
    Defaults to the current process user.
    """

    _job_parameter_values: JobParameterValues
    """Values for any defined Job Parameters.
    This is a dictionary.
        key = Parameter name (e.g. "Foo")
        value = Parameter's type and value
    """

    _cleanup_called: bool
    """Whether or not the application has called cleanup.
    If not, then we will call it automatically in __del__.
    """

    _callback: Optional[SessionCallbackType]
    """If provided, then this callback will be invoked on every:
        1. Open Job Description action message in the log (lines that start with openjd_)
        2. Completion/exit of the current action.
    The callback takes two arguments:
        - The session_id of this session; and
        - The updated value of Session.action_status; this contains the
            runtime state of the Action (running, etc) and optionally the
            progress, exit_code, and any output messages.
    """

    _path_mapping_rules: Optional[list[PathMappingRule]]
    """A list of the Path Mapping rules to communicate to Actions run via this Session.
    """

    _session_root_directory: Optional[Path]
    """If non-None, then this is the directory within-which the Session creator wants
    the session's working directory to be created.
    """

    # Status fields for the currently running process, if any.
    _action_state: Optional[ActionState]
    _action_progress: Optional[float]
    _action_status_message: Optional[str]
    _action_fail_message: Optional[str]
    _action_exit_code: Optional[int]

    def __init__(
        self,
        *,
        session_id: str,
        job_parameter_values: JobParameterValues,
        path_mapping_rules: Optional[list[PathMappingRule]] = None,
        retain_working_dir: bool = False,
        user: Optional[SessionUser] = None,
        callback: Optional[SessionCallbackType] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        session_root_directory: Optional[Path] = None,
        revision_extensions: RevisionExtensions = RevisionExtensions(
            spec_rev=SpecificationRevision.v2023_09, supported_extensions=[]
        ),
    ):
        """
        Arguments:
            session_id (str): An application-defined string value with which the session is identified.
            job_parameter_values (JobParameterValues): Values for any defined Job Parameters. This is a
                dictionary where the keys are parameter names, and the values are instances of
                ParameterValue (a dataclass containing the type and value of the parameter)
            path_mapping_rules (Optional[list[PathMappingRule]]): A list of the path mapping rules to apply
                within all actions running within this session. Defaults to None.
            retain_working_dir (bool, optional): If set, then the Session's Working Directory
                is not deleted when this Session object is deleted. Defaults to False.
            user (Optional[SessionUser]): The specific OS user to run subprocesses as, and whom
                will have permissions to the Session's Working Directory.
                Defaults to the current process user.
            callback (Optional[SessionCallbackType]): If provided, then this callback will be
                invoked on every:
                    1. Start of an Action (when the subprocess is actually running);
                    2. Open Job Description action message in the log (lines that start with openjd_)
                    3. Completion/exit of the current action.
                The callback takes two arguments:
                    - The session_id of this session; and
                    - The updated value of Session.action_status; this contains the
                        runtime state of the Action (running, etc) and optionally the
                        progress, exit_code, and any output messages.
                An implementation that uses this callback must ensure that the callback exits
                very rapidly; not doing so will delay processing of stdout/stderr of running
                subprocesses.
                WARNING: This callback may be called from the same thread that initiated
                a Script run in this session. This may happen if an error was encountered
                prior to trying to run the Action (e.g. failing to write an embedded file to
                disk); if the Action runs very quickly; or other similar circumstances.
            os_env_vars (Optional[dict[str,str]]): Definitions for additional OS Environment
                Variables that should be injected into all running processes in the Session.
                    Key: Environment variable name
                    Value: Value for the environment variable.
            session_root_directory (Optional[Path]): If provided, then:
                1. The given directory must already exist;
                2. The 'user' (if given) must have at least read permissions to it; and
                3. The Working Directory for this Session will be created in the given directory.
                If not provided, then the default of gettempdir()/"openjd" is used instead.
            revision_extensions (RevisionExtensions): Specification revision and supported extensions
                for this session. Defaults to SpecificationRevision.v2023_09 with no extensions.

        Raises:
            RuntimeError - If the Session initialization fails for any reason.
        """

        self._session_id = session_id
        self._ending_only = False
        self._environments = dict()
        self._environments_entered = list()
        self._runner = None
        self._running_environment_identifier = None
        self._process_env = dict(os_env_vars) if os_env_vars else dict()
        self._created_env_vars = dict()
        self._retain_working_dir = retain_working_dir
        self._user = user
        self._job_parameter_values = dict(job_parameter_values) if job_parameter_values else dict()
        self._cleanup_called = False
        self._callback = callback
        self._path_mapping_rules = path_mapping_rules[:] if path_mapping_rules else None
        if self._path_mapping_rules is not None:
            # Path mapping rules are applied in order of longest to shortest source path,
            # so sort them for when we apply them.
            self._path_mapping_rules.sort(key=lambda rule: -len(rule.source_path.parts))
        self._session_root_directory = session_root_directory
        if self._session_root_directory is not None:
            if not self._session_root_directory.is_dir():
                raise RuntimeError(
                    f"Ensure that the root directory ({str(self._session_root_directory)}) exists and is a directory."
                )
        self._reset_action_state()

        # Store the revision_extensions
        self._revision_extensions = revision_extensions

        # Set up our logging hook & callback
        self._log_filter = ActionMonitoringFilter(
            session_id=self._session_id,
            callback=self._action_log_filter_callback,
            revision_extensions=revision_extensions,
        )
        LOG.addFilter(self._log_filter)
        self._logger = LoggerAdapter(LOG, extra={"session_id": self._session_id})

        host_info_extra = LogExtraInfo(openjd_log_content=LogContent.HOST_INFO)
        self._logger.info(f"openjd.model Library Version: {model_version}", extra=host_info_extra)
        self._logger.info(f"openjd.sessions Library Version: {version}", extra=host_info_extra)
        self._logger.info(
            "Installed at: %s", str(Path(__file__).resolve().parent.parent), extra=host_info_extra
        )
        self._logger.info(f"Python Interpreter: {sys.executable}", extra=host_info_extra)
        self._logger.info(
            "Python Version: %s", sys.version.replace("\n", " - "), extra=host_info_extra
        )
        self._logger.info(f"Platform: {sys.platform}", extra=host_info_extra)
        self._logger.info(f"Initializing Open Job Description Session: {self._session_id}")

        try:
            self._working_dir = self._create_working_directory()
            self._files_dir = self._create_files_directory()
        except RuntimeError as exc:
            self._logger.error(
                f"ERROR creating Session Working Directory: {str(exc)}",
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.COMMAND_OUTPUT | LogContent.FILE_PATH
                ),
            )
            self._state = SessionState.ENDED
            raise

        self._logger.info(
            f"Session Working Directory: {str(self.working_directory)}",
            extra=LogExtraInfo(openjd_log_content=LogContent.FILE_PATH),
        )
        self._logger.info(
            f"Session's Embedded Files Directory: {str(self.files_directory)}",
            extra=LogExtraInfo(openjd_log_content=LogContent.FILE_PATH),
        )

        self._state = SessionState.READY

    def cleanup(self) -> None:
        """Cleanup all resources created by this Session"""
        if self._cleanup_called:
            return
        self._cleanup_called = True
        if self._working_dir is not None and not self._retain_working_dir:
            log_section_banner(self._logger, "Session Cleanup")
            self._logger.info(
                f"Deleting working directory: {str(self.working_directory)}",
                extra=LogExtraInfo(openjd_log_content=LogContent.FILE_PATH),
            )
            try:
                # If running as a different user, then that user could have written files to the
                # session diretory that make removing it as our user impossible. So, do a 2-phase
                # removal: 1/ `sudo -u <user> -i rm -rf <sessiondir>`, and then 2/ doing a normal
                # recursive removal to delete the stuff that only this user can delete.
                if self._user is not None:
                    files = [str(f) for f in self.working_directory.glob("*")]

                    creation_flags = None
                    if is_posix():
                        recursive_delete_cmd = ["rm", "-rf"]
                    else:
                        recursive_delete_cmd = [
                            "powershell",
                            "-Command",
                            "Remove-Item",
                            "-Recurse",
                            "-Force",
                        ]
                        files = [", ".join(files)]
                        # The cleanup needs to run as a high priority
                        # https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getpriorityclass#return-value
                        creation_flags = HIGH_PRIORITY_CLASS

                    _subprocess = LoggingSubprocess(
                        logger=self._logger,
                        args=recursive_delete_cmd + files,
                        user=self._user,
                        creation_flags=creation_flags,
                    )
                    # Note: Blocking call until the process has exited
                    _subprocess.run()

                self._working_dir.cleanup()
            except RuntimeError as exc:
                # Warn if we couldn't cleanup the temporary files for some reason.
                self._logger.exception(
                    exc,
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.EXCEPTION_INFO | LogContent.FILE_PATH
                    ),
                )

        LOG.removeFilter(self._log_filter)
        del self._log_filter
        if self._runner:
            self._runner.shutdown()
            self._runner = None
        self._state = SessionState.ENDED

    def __enter__(self) -> "Session":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.cleanup()

    # ========================
    #  Properties

    @property
    def working_directory(self) -> Path:
        """The directory that was created for this Session's working files.
        This is available in a Job Template's format string expressions as
        Session.WorkingDirectory
        """
        assert self._working_dir is not None
        return self._working_dir.path

    @property
    def files_directory(self) -> Path:
        """The subdirectory of the working_directory where files that have
        been inlined into a Job Template are stored.
        """
        return self._files_dir.path

    @property
    def state(self) -> SessionState:
        """Fetch the current state of this Session."""
        return self._state

    @property
    def action_status(self) -> Optional[ActionStatus]:
        """Obtain the status of the currently running, or previously running, Action
        Includes progress, status messages, and exit code; if they are available.
        """
        # If we don't have an action state, then we're not running or haven't run
        # anything yet.
        if self._action_state is None:
            return None
        return ActionStatus(
            state=self._action_state,
            progress=self._action_progress,
            status_message=self._action_status_message,
            fail_message=self._action_fail_message,
            exit_code=self._action_exit_code,
        )

    @property
    def environments_entered(self) -> tuple[EnvironmentIdentifier, ...]:
        """Returns an immutable list of the identifiers for Environments
        that have been entered, in the order that that have been entered.
        """
        return tuple(self._environments_entered)

    # =========================
    #  Running Actions

    def cancel_action(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed=False
    ) -> None:
        """Initiate a cancelation of the currently running Script if there is one.

        Arguments:
            time_limit (Optional[timedelta]): If provided, then the cancel must be completed within
                the given number of seconds. This overrides the current action's cancelation method
                and is intended for urgent cancels (e.g. in response to the controlling process
                getting a SIGTERM). Note: a value of 0 turns a notify-then-terminate cancel into a
                terminate

        Raises:
            RuntimeError: When there is no Script running.
        """
        if self.state != SessionState.RUNNING:
            raise RuntimeError("No actions are running")
        # For the type checker
        assert self._runner is not None

        self._runner.cancel(time_limit=time_limit, mark_action_failed=mark_action_failed)

    def enter_environment(
        self,
        *,
        environment: EnvironmentModel,
        identifier: Optional[EnvironmentIdentifier] = None,
        os_env_vars: Optional[dict[str, str]] = None,
    ) -> EnvironmentIdentifier:
        """Enters an Open Job Description Environment within this Session.
        This method is non-blocking; it will exit when the subprocess is either confirmed to have
        started running, or has failed to be started.

        Arguments:
            environment (EnvironmentScriptModel): An Environment Script from a supported
                Open Job Description schema version. This Script should not have any of its Format Strings
                evaluated before hand.
            identifier (Optional[EnvironmentIdentifier]): If provided then this is the identifier
                that the Environment will be known by to this Session.
                Default: An identifier is randomly generated.
            os_env_vars (Optional[dict[str,str]): Definitions for additional OS Environment
                Variables that should be injected into the process that is run for this action.
                Values provided override values provided to the Session constructor, and are overriden
                by values defined in Environments.
                    Key: Environment variable name
                    Value: Value for the environment variable.

        Returns:
            EnvironmentIdentifier: An identifier by which the Environment is known by to this Session.
                Pass this identifier to exit_environment() when exiting this Environment.
        """
        if self.state != SessionState.READY:
            raise RuntimeError("Session must be in the READY state to enter an Environment.")
        if identifier is not None and identifier in self._environments:
            raise RuntimeError(
                f"Environment {identifier} has already been entered in this Session."
            )
        log_section_banner(self._logger, f"Entering Environment: {environment.name}")

        self._reset_action_state()

        if identifier is None:
            identifier = f"{self._session_id}:{uuid.uuid4().hex}"

        self._environments[identifier] = environment
        self._environments_entered.append(identifier)
        self._running_environment_identifier = identifier

        symtab = self._symbol_table(environment.revision)

        if environment.variables is not None:
            # We must process the current environment's variables
            # before we call _evaluate_current_session_env_vars()
            # otherwise, we will end up running onEnter without
            # the environment variables of the current environment
            # being set.
            resolved_variables = self._resolve_env_variable_format_strings(
                symtab, environment.variables
            )
            for name, value in resolved_variables.items():
                self._logger.info(
                    "Setting: %s=%s",
                    name,
                    value,
                    extra=LogExtraInfo(openjd_log_content=LogContent.PARAMETER_INFO),
                )
            env_var_changes = SimplifiedEnvironmentVariableChanges(resolved_variables)
            self._created_env_vars[identifier] = env_var_changes
        else:
            # Running the environment may define environment variable
            # mutations via its stdout. We create an empty env changes
            # object to capture these.
            self._created_env_vars[identifier] = SimplifiedEnvironmentVariableChanges(
                dict[str, str]()
            )

        # Must be called _after_ we append to _environments_entered
        action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)

        self._materialize_path_mapping(environment.revision, action_env_vars, symtab)

        # Sets the subprocess running.
        # Returns immediately after it has started, or is running
        self._action_state = ActionState.RUNNING
        self._state = SessionState.RUNNING
        # Note: This may fail immediately (e.g. if we cannot write embedded files to disk),
        # so it's important to set the action_state to RUNNING before calling enter(), rather
        # than after -- enter() itself may end up setting the action state to FAILED.

        self._runner = EnvironmentScriptRunner(
            logger=self._logger,
            user=self._user,
            os_env_vars=action_env_vars,
            session_working_directory=self.working_directory,
            startup_directory=self.working_directory,
            callback=self._action_callback,
            environment_script=environment.script,
            symtab=symtab,
            session_files_directory=self.files_directory,
        )
        self._runner.enter()

        return identifier

    def exit_environment(
        self,
        *,
        identifier: EnvironmentIdentifier,
        os_env_vars: Optional[dict[str, str]] = None,
        keep_session_running: bool = False,
    ) -> None:
        """Exits an Open Job Description Environment from this Session.
        This method is non-blocking; it will exit when the subprocess is either confirmed to have
        started running, or has failed to be started.

        Note that Environments *MUST* be exited in the opposite order in which they were entered.
        It is an error to do otherwise.

        Arguments:
            identifier (EnvironmentIdentifier): The identifier of the previously entered
                Environment to exit.
            os_env_vars (Optional[dict[str,str]): Definitions for additional OS Environment
                Variables that should be injected into the process that is run for this action.
                Values provided override values provided to the Session constructor, and are overriden
                by values defined in Environments.
                    Key: Environment variable name
                    Value: Value for the environment variable.
            keep_session_running (bool): This overrides the default of requiring only environment exits after
                the first exit_environment is called. The caller can set this to True in order to exit
                the environments of a step and then run tasks from a different step.

        Raises:
            ValueError - If the given identifier is not that of the next one that must be exited.
        """
        if self.state != SessionState.READY and self.state != SessionState.READY_ENDING:
            raise RuntimeError(
                "Session must be in the READY or READY_ENDING state to exit an Environment."
            )
        if identifier not in self._environments:
            raise RuntimeError(f"Cannot exit unknown Environment with identifier {identifier}")
        if self._environments_entered[-1] != identifier:
            raise RuntimeError(
                f"Cannot exit Environment {identifier}. Must exit Environment {self._environments_entered[-1]} first."
            )

        self._reset_action_state()

        # Unless overridden by the caller, once we've started exiting environments, then we can only exit environments.
        if not keep_session_running:
            self._ending_only = True

        environment = self._environments[identifier]
        log_section_banner(self._logger, f"Exiting Environment: {environment.name}")

        # Must be run _before_ we pop _environments_entered
        action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)

        # Remove the environment from our tracking since we're now exiting it.
        del self._environments[identifier]
        self._environments_entered.pop()

        self._running_environment_identifier = identifier

        symtab = self._symbol_table(environment.revision)
        self._materialize_path_mapping(environment.revision, action_env_vars, symtab)
        # Sets the subprocess running.
        # Returns immediately after it has started, or is running
        self._action_state = ActionState.RUNNING
        self._state = SessionState.RUNNING
        # Note: This may fail immediately (e.g. if we cannot write embedded files to disk),
        # so it's important to set the action_state to RUNNING before calling exit(), rather
        # than after -- exit() itself may end up setting the action state to FAILED.

        self._runner = EnvironmentScriptRunner(
            logger=self._logger,
            user=self._user,
            os_env_vars=action_env_vars,
            session_working_directory=self.working_directory,
            startup_directory=self.working_directory,
            callback=self._action_callback,
            environment_script=environment.script,
            symtab=symtab,
            session_files_directory=self.files_directory,
        )
        self._runner.exit()

    def run_task(
        self,
        *,
        step_script: StepScriptModel,
        task_parameter_values: TaskParameterSet,
        os_env_vars: Optional[dict[str, str]] = None,
        log_task_banner: bool = True,
    ) -> None:
        """Run a Task within the Session.
        This method is non-blocking; it will exit when the subprocess is either confirmed to have
        started running, or has failed to be started.

        Arguments:
            step_script (StepScriptModel): The Step Script that the Task will be running.
            task_parameter_values (TaskParameterSet): Values of the Task parameters that define the
                specific Task. This is a dictionary where the keys are parameter names, and the values
                are instances of ParameterValue (a dataclass containing the type and value of the parameter)
            os_env_vars (Optional[dict[str,str]): Definitions for additional OS Environment
                Variables that should be injected into the process that is run for this action.
                Values provided override values provided to the Session constructor, and are overriden
                by values defined in Environments.
                    Key: Environment variable name
                    Value: Value for the environment variable.
            log_task_banner (bool): Whether to log a banner before running the Task.
                Default: True
        """
        if self.state != SessionState.READY:
            raise RuntimeError("Session must be in the READY state to run a task.")

        if log_task_banner:
            log_section_banner(self._logger, "Running Task")

        if task_parameter_values:
            self._logger.info(
                "Parameter values:",
                extra=LogExtraInfo(openjd_log_content=LogContent.PARAMETER_INFO),
            )
            for name, value in task_parameter_values.items():
                self._logger.info(
                    f"{name}({str(value.type.value)}) = {value.value}",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PARAMETER_INFO),
                )

        self._reset_action_state()
        symtab = self._symbol_table(step_script.revision, task_parameter_values)
        action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)
        self._materialize_path_mapping(step_script.revision, action_env_vars, symtab)
        self._runner = StepScriptRunner(
            logger=self._logger,
            user=self._user,
            os_env_vars=action_env_vars,
            session_working_directory=self.working_directory,
            startup_directory=self.working_directory,
            callback=self._action_callback,
            script=step_script,
            symtab=symtab,
            session_files_directory=self.files_directory,
        )
        # Sets the subprocess running.
        # Returns immediately after it has started, or is running
        self._action_state = ActionState.RUNNING
        self._state = SessionState.RUNNING
        # Note: This may fail immediately (e.g. if we cannot write embedded files to disk),
        # so it's important to set the action_state to RUNNING before calling run(), rather
        # than after -- run() itself may end up setting the action state to FAILED.
        self._runner.run()

    # =========================
    #  Helpers

    def get_enabled_extensions(self) -> list[str]:
        """Return the list of enabled extensions for this session.

        Returns:
            list[str]: The list of enabled extensions
        """
        return list(self._revision_extensions.extensions)

    def _reset_action_state(self) -> None:
        """Reset the internal action state.
        This resets to a state equivalent to having nothing running.
        """
        self._action_state = None
        self._action_progress = None
        self._action_status_message = None
        self._action_fail_message = None
        self._action_exit_code = None
        self._running_environment_identifier = None
        if self._runner:
            self._runner.shutdown()
            self._runner = None

    def _symbol_table(
        self,
        version: SpecificationRevision,
        task_parameter_values: Optional[TaskParameterSet] = None,
    ) -> SymbolTable:
        """Construct a SymbolTable, with fully qualified value names, suitable for running a Script."""

        def processed_parameter_value(param: ParameterValue) -> str:
            if param.type == ParameterValueType.PATH and self._path_mapping_rules is not None:
                # Apply path mapping rules in the order given until one does a replacement
                for rule in self._path_mapping_rules:
                    changed, result = rule.apply(path=param.value)
                    if changed:
                        return result
            return param.value

        if version == SpecificationRevision.v2023_09:
            symtab = SymbolTable()
            symtab[ValueReferenceConstants_2023_09.WORKING_DIRECTORY.value] = str(
                self.working_directory
            )
            for param_name, param_props in self._job_parameter_values.items():
                symtab[
                    f"{ValueReferenceConstants_2023_09.JOB_PARAMETER_RAWPREFIX.value}.{param_name}"
                ] = param_props.value
                symtab[
                    f"{ValueReferenceConstants_2023_09.JOB_PARAMETER_PREFIX.value}.{param_name}"
                ] = processed_parameter_value(param_props)
            if task_parameter_values:
                for param_name, param_props in task_parameter_values.items():
                    symtab[
                        f"{ValueReferenceConstants_2023_09.TASK_PARAMETER_RAWPREFIX.value}.{param_name}"
                    ] = param_props.value
                    symtab[
                        f"{ValueReferenceConstants_2023_09.TASK_PARAMETER_PREFIX.value}.{param_name}"
                    ] = processed_parameter_value(param_props)
            return symtab
        else:
            raise NotImplementedError(f"Schema version {str(version.value)} is not supported.")

    def _openjd_session_root_dir(self) -> Path:
        """
        Returns (and creates if necessary) the top-level directory where Open Job Description step session
        directories are kept
        """
        if self._session_root_directory is not None:
            return self._session_root_directory

        tempdir = Path(custom_gettempdir(self._logger))

        # Note: If this doesn't have group permissions, then we will be unable to access files
        #  under this directory if the default group of the current user is the group that
        #  is shared with a job user. The group permissions override the world permissions
        #  when the accessor is in the group.
        # 0o755
        mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        tempdir.mkdir(exist_ok=True, mode=mode)
        # tempdir might already exist with the incorrect permissions, so set the permissions.
        os.chmod(tempdir, mode=mode)
        return tempdir

    def _create_working_directory(self) -> TempDir:
        """Creates and returns the temporary working directory for this Session"""
        root_dir = self._openjd_session_root_dir()

        if os_name == "posix":
            # Check the sticky bit. If we have any world-writeable parents to
            # the root_dir that don't have the sticky bit set, then
            # the system has an insecure setup for multiuser systems.
            for parent in root_dir.parents:
                parent_stat = os_stat(parent)
                # Note There is a nuanced security risks to putting the session directory in a world-writable parent
                # directory. Normally, users with write permissions to a directory can delete files/directories within
                # that directory and this is a problem for world-writable dirs like /tmp. Linux distros typically
                # default to the system temp dir having the sticky bit set which restricts deletion of files/dirs in
                # world-writable dirs to only the owning user or a privileged/root user. Not all distros may respect this,
                # or system administrators may unset the sticky bit.
                if (parent_stat.st_mode & stat.S_IWOTH) != 0 and (
                    parent_stat.st_mode & stat.S_ISVTX
                ) == 0:
                    self._logger.warning(
                        f"Sticky bit is not set on {str(parent)}. This may pose a risk when running work on this host as users may modify or delete files in this directory which do not belong to them.",
                        extra=LogExtraInfo(
                            openjd_log_content=LogContent.HOST_INFO | LogContent.FILE_PATH
                        ),
                    )

        # Raises: RuntimeError
        return TempDir(dir=root_dir, prefix=self._session_id, user=self._user, logger=self._logger)

    def _create_files_directory(self) -> TempDir:
        """Creates the subdirectory of the working directory in which we'll materialize
        any embedded files from the Job Template."""
        # Raises: RuntimeError
        return TempDir(
            dir=self.working_directory,
            prefix="embedded_files",
            user=self._user,
            logger=self._logger,
        )

    def _materialize_path_mapping(
        self, version: SpecificationRevision, os_env: dict[str, Optional[str]], symtab: SymbolTable
    ) -> None:
        """Materialize path mapping rules to disk and the os environment variables."""
        if self._path_mapping_rules:
            rules_dict = {
                "version": "pathmapping-1.0",
                "path_mapping_rules": [
                    {
                        "source_path_format": rule.source_path_format.value,
                        "source_path": str(rule.source_path),
                        "destination_path": str(rule.destination_path),
                    }
                    for rule in self._path_mapping_rules
                ],
            }
            symtab[ValueReferenceConstants_2023_09.HAS_PATH_MAPPING_RULES.value] = "true"
        else:
            rules_dict = dict()
            symtab[ValueReferenceConstants_2023_09.HAS_PATH_MAPPING_RULES.value] = "false"
        rules_json = json.dumps(rules_dict)
        file_handle, filename = mkstemp(dir=self.working_directory, suffix=".json", text=True)
        os.close(file_handle)
        write_file_for_user(Path(filename), rules_json, self._user)
        symtab[ValueReferenceConstants_2023_09.PATH_MAPPING_RULES_FILE.value] = str(filename)

    def _resolve_env_variable_format_strings(
        self, symtab: SymbolTable, variables: "EnvironmentVariableObject"
    ) -> dict[str, str]:
        """When definining an environment variable via an Environment entity's "variables" declaration,
        the values of those variables are format strings that must be evaluated. Do that, and return the
        result.
        """
        result = dict()
        for name, value in variables.items():
            result[name] = value.resolve(symtab=symtab)

        return result

    def _action_log_filter_callback(
        self, kind: ActionMessageKind, value: Any, cancel_action_mark_failed: bool = False
    ) -> None:
        """This callback is invoked by the ActionMonitoringFilter that we've attached to the LOG.
        This will be called whenever an "openjd" message is detected in the log stream.
        This will be invoked by the main thread in LoggingSubprocess that is forwarding
        all stdout/stderr to the logs. Delays here delay that main loop from processing
        output.
        """
        if kind == ActionMessageKind.PROGRESS:
            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, float)
            self._action_progress = value
        elif kind == ActionMessageKind.STATUS:
            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, str)
            self._action_status_message = value
        elif kind == ActionMessageKind.FAIL:
            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, str)
            self._action_fail_message = value

            if cancel_action_mark_failed:
                # Cancel the action and pass the failure message
                self.cancel_action(mark_action_failed=True)

        elif kind == ActionMessageKind.ENV:
            if self._running_environment_identifier is None:
                # Ignore the message if we're not running an environment
                return
            if cancel_action_mark_failed:
                # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
                assert isinstance(value, str)

                # Cancel the action and pass the failure message
                self.cancel_action(mark_action_failed=True)
                self._action_fail_message = value

                return
            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, dict)
            # value = { "name": <name>, "value": <value> }
            env_vars = self._created_env_vars[self._running_environment_identifier]
            env_vars.simplify_ordered_changes(
                changes=[EnvironmentVariableSetChange(name=value["name"], value=value["value"])]
            )
            return
        elif kind == ActionMessageKind.UNSET_ENV:
            if self._running_environment_identifier is None:
                # Ignore the message if we're not running an environment
                return

            if cancel_action_mark_failed:
                # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
                assert isinstance(value, str)

                # Cancel the action and pass the failure message
                self.cancel_action(mark_action_failed=True)
                self._action_fail_message = value

                return

            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, str)
            env_vars = self._created_env_vars[self._running_environment_identifier]
            env_vars.simplify_ordered_changes(changes=[EnvironmentVariableUnsetChange(name=value)])
            return
        else:  # ActionMessageKind.SESSION_RUNTIME_LOGLEVEL
            assert isinstance(value, int)
            self._logger.setLevel(value)
            return

        if self._callback:
            action_status = self.action_status
            # for the type checker
            assert action_status is not None
            self._callback(self._session_id, action_status)

    def _action_callback(self, state: ActionState) -> None:
        """This callback is invoked:
        1. When the Action process is successfully started, by the same thread that is running the
           process (so, this holds up IO processing);
        2. *After* the "run future" in ScriptRunnerBase has exited, by the same thread that was
           running that "run future";
        3. If we failed *before* actually running the Action (e.g. while trying to write embedded
           files to disk) then this will be invoked by the same thread that called Session.environment_*()
           or Session.run_task().

        We can be certain that the process is no longer running when this is called.
        """
        # For the type checker
        assert self._runner is not None

        self._action_exit_code = self._runner.exit_code
        self._action_state = state

        if state != ActionState.RUNNING:
            # Decide which between-action state to enter.
            if self._ending_only or self._action_state != ActionState.SUCCESS:
                # Sessions are "brittle". If there's a Task cancel or Failure then
                # we can only exit the Session.
                self._state = SessionState.READY_ENDING
            else:
                self._state = SessionState.READY

        if self._callback:
            action_status = self.action_status
            # for the type checker
            assert action_status is not None
            self._callback(self._session_id, action_status)

    def _evaluate_current_session_env_vars(
        self, extra_env_vars: Optional[dict[str, str]] = None
    ) -> dict[str, Optional[str]]:
        """Get a dictionary representing the cummulative state of env vars set
        and unset from the currently applied environments.
        """
        result = dict[str, Optional[str]](self._process_env)  # Make a copy
        if extra_env_vars:
            result.update(**extra_env_vars)
        for identifier in self._environments_entered:
            if identifier in self._created_env_vars:
                self._created_env_vars[identifier].apply_to_environment(result)
        return result
