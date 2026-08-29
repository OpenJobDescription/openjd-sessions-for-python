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
from typing import TYPE_CHECKING, Any, Callable, Optional, Type, Union, cast

from openjd.model import (
    FormatStringError,
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
    Action as Action_2023_09,
    ArgString as ArgString_2023_09,
    CancelationMethodTerminate as CancelationMethodTerminate_2023_09,
    CancelationMode as CancelationMode_2023_09,
    CommandString as CommandString_2023_09,
    StepActions as StepActions_2023_09,
    StepScript as StepScript_2023_09,
    ValueReferenceConstants as ValueReferenceConstants_2023_09,
)
from ._action_filter import ActionMessageKind, ActionMonitoringFilter
from ._embedded_files import EmbeddedFiles, EmbeddedFilesScope, _FileRecord, write_file_for_user
from ._logging import LOG, log_section_banner, LoggerAdapter, LogExtraInfo, LogContent
from ._os_checker import is_posix, is_windows
from ._path_mapping import PathMappingRule
from ._runner_base import (
    ScriptRunnerBase,
    apply_script_let_bindings,
    resolve_action_arg_values,
    resolve_effective_cancelation,
    resolve_optional_int_field,
)
from ._runner_env_script import EnvironmentScriptRunner, WRAP_HOOK_ACTION_NAMES
from ._runner_step_script import StepScriptRunner
from ._session_user import SessionUser
from ._subprocess import LoggingSubprocess
from ._tempdir import TempDir, custom_gettempdir
from ._types import (
    ENV_ACTION_DEFAULT_NOTIFY_PERIOD_SECONDS,
    TASK_RUN_DEFAULT_NOTIFY_PERIOD_SECONDS,
    ActionState,
    EnvironmentIdentifier,
    EnvironmentModel,
    EnvironmentScriptModel,
    StepScriptModel,
)
from ._version import version

if is_windows():  # pragma: nocover
    from subprocess import HIGH_PRIORITY_CLASS  # type: ignore

if TYPE_CHECKING:
    from openjd.expr import SerializedSymbolTable
    from openjd.model.v2023_09._model import EnvironmentVariableObject

__all__ = ("SessionState", "Session", "EnvironmentIdentifier")


SESSION_DIR_NAME_LENGTH = 8
"""Length of a session working directory's name.

- Every character is charged against MAX_PATH (260) for the applications a job
  runs, which are not long-path aware, so `LongPathsEnabled` and `\\\\?\\` do not
  help them.
- The name is mkdtemp()'s 8 random characters and nothing else. Passing the
  40-character session id as a prefix cost 48.
- The id was pure label there: mkdtemp() generates the random characters and
  creates each candidate with an exclusive mkdir(), retrying under a taken name.
- Directory -> session stays recoverable from the session log, which the Worker
  Agent writes to `<worker_logs_dir>/<queue_id>/<session_id>.log` and which
  outlives the directory. See _create_working_directory.
"""


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
        # Insertion-ordered map of every session-defined variable for this
        # environment: the declarative `variables:` map seed plus any
        # openjd_env stdout sets/unsets (None = unset). RFC 0008's
        # ``WrappedAction.Environment`` surfaces all of these (openjd-rs
        # #277); host-inherited variables never enter this map.
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
            name = change.name.upper() if is_windows() else change.name
            if isinstance(change, EnvironmentVariableSetChange):
                self._to_set[name] = change.value
            elif isinstance(change, EnvironmentVariableUnsetChange):
                self._to_set[name] = None
            else:
                raise ValueError("Unknown type of environment variable change.")

    def effective_items(self) -> dict[str, Optional[str]]:
        """The effective session-defined variables for this environment, in
        insertion order: the declarative ``variables:`` map seed plus any
        openjd_env stdout sets/unsets applied so far (``None`` = unset).
        Returns a copy; mutating it does not affect this object."""
        return dict(self._to_set)

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

    _session_env_vars: dict[str, str]
    """Session-defined environment variables, for the lifetime of the Session.

    Deliberately separate from :attr:`_created_env_vars`, which is keyed by
    environment and read through ``_environments_entered`` to build the child
    *process* environment -- that view must shrink when an environment exits.
    This one must not: RFC 0008 (``rfcs/0008-environment-wrap-actions.md``,
    "MUST include in ``WrappedAction.Environment`` every ``openjd_env``-defined
    variable emitted by any earlier action in the same session") makes
    session-lifetime inclusion a requirement, so a wrap script can forward a
    variable exported by an environment that has since exited.

    The only remover is an explicit ``openjd_unset_env``. openjd-rs holds the
    same split -- its session-lifetime ``env_vars`` beside its per-environment
    ``created_env_vars`` -- and this mirrors it.
    """

    _wrap_env_file_records: dict[EnvironmentIdentifier, list["_FileRecord"]]
    """RFC 0008: per wrap environment, the embedded-file records whose on-disk
    paths were allocated on the environment's first wrap-hook invocation and
    are reused for every subsequent invocation — so the ``Env.File.*`` symbols
    stay stable across tasks and unnamed files do not accumulate on disk. The
    file *contents* are still re-resolved and rewritten per invocation by the
    runner — not because they can change (validation rejects ``WrappedAction.*``
    in an environment script's ``data`` and ``let``, so they cannot), but so that
    every invocation starts from the authored content even if a previous one
    modified the file on disk. See ScriptRunnerBase._materialize_files.
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
        job_name: Optional[str] = None,
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
            job_name (Optional[str]): The resolved name of the Job this Session belongs to.
                When provided, it is seeded as the ``Job.Name`` template variable
                (RFC 0005; Template Schemas §7.3.1, EXPR extension) for the session's actions —
                mirroring the Job.Name symbol that openjd-rs carries in its
                session symbol tables. Defaults to None (no Job.Name symbol).
            path_mapping_rules (Optional[list[PathMappingRule]]): A list of the path mapping rules to apply
                within all actions running within this session. Defaults to None.
            retain_working_dir (bool, optional): If set, then the Session's Working Directory
                is not deleted when this Session object is deleted. Defaults to False.
                Note: the working directory's *name* no longer encodes the session id
                (it is mkdtemp()'s random characters and nothing else), so a retained
                directory is not attributable to a session by its name alone. The
                session log records the working directory's full path against the
                session id; that log is how a retained or orphaned directory is
                resolved back to its session.
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
        # The owning step's name supplied when an environment was entered
        # (seeds Step.Name, RFC 0005 EXPR), re-seeded when the environment
        # exits so its onExit resolves in the same scope as its onEnter.
        self._environment_step_names: dict[EnvironmentIdentifier, str] = dict()
        # The converted service-resolved base an environment was entered with.
        # A wrap environment's hooks resolve in its enter-time scope, so
        # _seed_wrap_env_scope re-seeds this into every hook scope, mirroring
        # openjd-rs merging the environment's frozen resolved table onto the
        # hook's table.
        self._environment_resolved_bases: dict[EnvironmentIdentifier, dict[str, Any]] = dict()
        self._environments_entered = list()
        self._runner = None
        self._running_environment_identifier = None
        self._process_env = dict(os_env_vars) if os_env_vars else dict()
        self._created_env_vars = dict()
        self._session_env_vars = dict()
        self._wrap_env_file_records = dict()
        self._retain_working_dir = retain_working_dir
        self._user = user
        self._job_parameter_values = dict(job_parameter_values) if job_parameter_values else dict()
        self._job_name = job_name
        self._cleanup_called = False
        self._callback = callback
        self._path_mapping_rules = path_mapping_rules[:] if path_mapping_rules else None
        if self._path_mapping_rules is not None:
            # Path mapping rules are applied in order of longest to shortest source path,
            # so sort them for when we apply them.
            self._path_mapping_rules.sort(key=lambda rule: -rule._source_path_component_count())
        # Engine (openjd.expr) form of the path mapping rules, seeded into the
        # session's symbol tables so EXPR host-context functions such as
        # apply_path_mapping() apply the session's rules at run time —
        # mirroring openjd-rs's session-scope HostContext::WithRules. An empty
        # list still constructs a host context (the session IS host scope);
        # the rules are simply empty.
        self._expr_host_rules = self._build_expr_host_rules()
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

        # Expose working directory as env var for nested subprocesses that can't access template variables.
        # This env var is part of the public API — removing or renaming it is a breaking change.
        self._process_env["OPENJD_SESSION_WORKING_DIR"] = str(self.working_directory)

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

        The directory's name is generated (mkdtemp()'s random characters) and
        carries no part of the session id, so do not parse identity out of it;
        the session id is recorded against this path in the session log instead.

        Raises:
            RuntimeError: If this Session has no working directory, which means
                construction did not complete.
        """
        # A public property, so an explicit raise rather than `assert`: under
        # `python -O` the assert vanished and the caller got
        # `AttributeError: 'NoneType' object has no attribute 'path'` from inside
        # a library property (R5-6).
        if self._working_dir is None:
            raise RuntimeError(
                "This Session has no working directory; its construction did not complete."
            )
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
        # Review22-F3 fix: Snapshot _runner before using it. The state check
        # above and the _runner access below are not atomic; a completion
        # racing this call could set _runner = None after we pass the state
        # check. Snapshotting here avoids a bare AssertionError.
        runner = self._runner
        if runner is None:
            # Race: action completed between state check and here. No-op rather
            # than raise, since the caller's intent (cancel the running action)
            # is already satisfied.
            return

        runner.cancel(time_limit=time_limit, mark_action_failed=mark_action_failed)

    def _make_env_script_runner(
        self,
        *,
        environment_script: Optional[EnvironmentScriptModel],
        os_env_vars: dict[str, Optional[str]],
        symtab: SymbolTable,
        preallocated_file_records: Optional[list[_FileRecord]] = None,
    ) -> EnvironmentScriptRunner:
        """Construct an :class:`EnvironmentScriptRunner` with this Session's
        standard wiring (logger, user, working/files directories, and action
        callback). Only the script, subprocess environment, symbol table, and
        (for wrap hooks) pre-allocated embedded-file records vary between the
        call sites."""
        return EnvironmentScriptRunner(
            logger=self._logger,
            user=self._user,
            os_env_vars=os_env_vars,
            session_working_directory=self.working_directory,
            startup_directory=self.working_directory,
            callback=self._action_callback,
            environment_script=environment_script,
            symtab=symtab,
            session_files_directory=self.files_directory,
            preallocated_file_records=preallocated_file_records,
        )

    def enter_environment(
        self,
        *,
        environment: EnvironmentModel,
        identifier: Optional[EnvironmentIdentifier] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        step_name: Optional[str] = None,
        resolved_symtab: Optional["SerializedSymbolTable"] = None,
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
            step_name (Optional[str]): The name of the step whose
                stepEnvironments are being entered, if any. Seeds
                ``Step.Name`` (RFC 0005 EXPR) into the symbol table so the
                environment's variables and actions can reference it.
                Redundant with ``resolved_symtab``, which already carries
                ``Step.Name``: kept for a caller that has a step name but no
                resolved table, since the table is otherwise the
                authoritative source of step scope.
            resolved_symtab (Optional[SerializedSymbolTable]): The step-scope
                symbol table generated by ``create_job`` (available as
                ``Step.resolved_symtab``). It contains ``Param.*``,
                ``RawParam.*``, ``Job.Name``, ``Step.Name``, and the
                step-level let-binding values. Its entries seed the session
                symbol table first, and the session's own values
                (``Session.WorkingDirectory``, path-mapped ``Param.*``)
                layer on top — the layering the openjd-rs runtime applies to
                the same table. ``None`` is fine when the environment script
                doesn't reference any of those names.

        Returns:
            EnvironmentIdentifier: An identifier by which the Environment is known by to this Session.
                Pass this identifier to exit_environment() when exiting this Environment.

        Raises:
            RuntimeError: If the Session is not in the READY state; if the given
                identifier has already been entered in this Session; or, per
                RFC 0008, if this Environment defines wrap hooks and another
                entered Environment already does.
        """
        if self.state != SessionState.READY:
            raise RuntimeError("Session must be in the READY state to enter an Environment.")
        if identifier is not None and identifier in self._environments:
            raise RuntimeError(
                f"Environment {identifier} has already been entered in this Session."
            )

        # RFC 0008: at most one Environment in the session stack may
        # define any wrap hook. Reject the new environment up front if it
        # would create a second wrap layer.
        if self._environment_defines_any_wrap_hook(environment):
            for existing_id in self._environments_entered:
                existing = self._environments[existing_id]
                if self._environment_defines_any_wrap_hook(existing):
                    raise RuntimeError(
                        "RFC 0008: a session may have at most one Environment "
                        "defining wrap hooks (onWrapEnvEnter / onWrapTaskRun / "
                        f"onWrapEnvExit). Environment '{existing.name}' already "
                        f"defines wrap hooks; cannot also enter '{environment.name}'."
                    )

        log_section_banner(self._logger, f"Entering Environment: {environment.name}")

        self._reset_action_state()

        if identifier is None:
            identifier = f"{self._session_id}:{uuid.uuid4().hex}"

        self._environments[identifier] = environment
        if step_name is not None:
            self._environment_step_names[identifier] = step_name
        self._environments_entered.append(identifier)
        self._running_environment_identifier = identifier

        # Deserialize the service-resolved base table (if given) before
        # building the symbol table, so its entries seed first.
        resolved_base: Optional[dict[str, Any]] = None
        if resolved_symtab is not None:
            try:
                resolved_base = self._resolved_base_entries(resolved_symtab)
            except ValueError as e:
                # Fail the action through the normal failure path rather than
                # raising out of the public API. The environment is already on
                # the entered list, so the caller's cleanup exits it as usual,
                # and the empty change record keeps the log-forwarding
                # thread's _created_env_vars lookup safe.
                self._created_env_vars[identifier] = SimplifiedEnvironmentVariableChanges(
                    dict[str, str]()
                )
                self._fail_action_before_start(
                    f"Failed to deserialize the resolved symbol table: {e}"
                )
                return identifier

        # Remembered for this environment's own wrap hooks (RFC 0008), which
        # resolve in its enter-time scope. Stored after the conversion, not
        # beside the step-name store above: that runs before it, so storing
        # there would leave a base behind on a failed deserialization.
        if resolved_base:
            self._environment_resolved_bases[identifier] = resolved_base

        symtab = self._symbol_table(environment.revision, resolved_base=resolved_base)

        # RFC 0005; Template Schemas §7.3.1 (EXPR): the owning step's name. Only EXPR templates
        # pass validation referencing Step.Name, so seeding it when known does
        # not change non-EXPR behavior. Applied after the resolved base seeds
        # so an explicit step_name wins over a stale base entry.
        if step_name is not None:
            symtab["Step.Name"] = step_name

        # Note: the environment script's own EXPR `let` bindings (RFC 0005)
        # are evaluated by the script runner, after embedded-file paths are
        # allocated (so bindings can reference Env.File.*). The environment's
        # `variables` resolve against the session symbol table without them,
        # matching the openjd-rs runtime.

        if environment.variables is not None:
            # We must process the current environment's variables
            # before we call _evaluate_current_session_env_vars()
            # otherwise, we will end up running onEnter without
            # the environment variables of the current environment
            # being set.
            try:
                resolved_variables = self._resolve_env_variable_format_strings(
                    symtab, environment.variables
                )
            except ValueError as e:
                # ExpressionError and FormatStringError subclass ValueError. A
                # variable's expression failed to evaluate at run time — which
                # EXPR (RFC 0005) makes reachable for a template that passed
                # validation, e.g. a host function applied to a value only known
                # at run time. Fail the action through the normal failure path
                # rather than raising out of the public API: the environment is
                # already in the entered list, so the caller must be given a
                # terminal ActionStatus *and* the identifier in order to exit it.
                #
                # Seeding the empty change record is required, not tidiness:
                # _action_log_filter_callback indexes _created_env_vars by the
                # running environment's identifier without a membership test, so
                # an openjd_env emitted by this environment's onExit would
                # otherwise raise KeyError on the log-forwarding thread.
                self._created_env_vars[identifier] = SimplifiedEnvironmentVariableChanges(
                    dict[str, str]()
                )
                self._fail_action_before_start(
                    f"Failed to resolve the environment variables for {environment.name}: {e}"
                )
                return identifier
            for name, value in resolved_variables.items():
                self._logger.info(
                    "Setting: %s=%s",
                    name,
                    value,
                    extra=LogExtraInfo(openjd_log_content=LogContent.PARAMETER_INFO),
                )
            env_var_changes = SimplifiedEnvironmentVariableChanges(resolved_variables)
            self._created_env_vars[identifier] = env_var_changes
            # Session-lifetime copy for WrappedAction.Environment. openjd-rs
            # writes declarative `variables:` into its session-lifetime
            # `env_vars` alongside `created_env_vars` at the same point.
            self._session_env_vars.update(resolved_variables)
        else:
            # Running the environment may define environment variable
            # mutations via its stdout. We create an empty env changes
            # object to capture these.
            self._created_env_vars[identifier] = SimplifiedEnvironmentVariableChanges(
                dict[str, str]()
            )

        # Must be called _after_ we append to _environments_entered
        action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)

        try:
            self._materialize_path_mapping(environment.revision, action_env_vars, symtab)
        except RuntimeError as e:
            self._fail_action_before_start(str(e))
            # The identifier is still returned on a pre-start failure: the
            # environment is on the entered stack, so the caller must be able to
            # exit it. Same contract as the resolve-variables failure above.
            return identifier

        # Note: RUNNING is set below, immediately before the runner is asked to
        # start, and never before `self._runner` exists. `cancel_action()` is a
        # cross-thread API guarded only by `state == RUNNING`, so a window where
        # the session claims RUNNING with no runner is a window in which a
        # cancel is lost. That matters here because the RFC 0008 branch does
        # real work first (materializing the inner entity's embedded files,
        # evaluating its `let` bindings, allocating the hook's file records).
        # Every failure before the launch goes through
        # `_fail_action_before_start`, which sets FAILED/READY_ENDING without
        # needing a prior RUNNING.

        # RFC 0008: an outer environment's onWrapEnvEnter intercepts an
        # inner environment's onEnter. The outer environment's *own*
        # onEnter is never wrapped — that's why the lookup excludes the
        # environment we just appended (which is at the top of the stack).
        on_enter_action = (
            environment.script.actions.onEnter if environment.script is not None else None
        )
        wrap_env = (
            self._find_wrap_environment(hook="onWrapEnvEnter")
            if on_enter_action is not None
            else None
        )
        # The wrapping environment is whichever earlier-entered env defines
        # onWrapEnvEnter — never the env we just entered.
        if wrap_env is environment:
            wrap_env = None

        if wrap_env is not None:
            # The wrapped onEnter resolves against the INNER environment's
            # own scope (`symtab`, which carries the resolved base and step
            # context that environment was entered with); the hook
            # resolves against its own table, which carries none of them -- see
            # _build_wrap_hook_scope. Its own script's lets/files are evaluated
            # into that table by the script runner from wrap_env.script. On
            # failure the environment stays in the entered list, exactly as
            # when enter() itself fails, so the caller's cleanup exits it
            # as usual.
            hook_symtab = self._build_wrap_hook_scope(
                environment.revision, symtab, resolved_base=resolved_base
            )
            if not self._try_inject_wrapped_symbols(
                scope=EmbeddedFilesScope.ENV,
                inner_script=environment.script,
                symtab=symtab,
                inject=lambda inner_symtab: self._inject_wrapped_env_symbols(
                    hook_symtab, environment, on_enter_action, inner_symtab=inner_symtab
                ),
                fail_message=(
                    f"Failed to resolve the wrapped onEnter action of "
                    f"{environment.name} for {wrap_env.name}'s onWrapEnvEnter"
                ),
            ):
                return identifier
            self._seed_wrap_env_scope(hook_symtab, wrap_env)
            try:
                wrap_file_records = self._get_wrap_env_file_records(wrap_env)
            except RuntimeError as e:
                self._fail_action_before_start(
                    f"Failed to allocate embedded files for {wrap_env.name}: {e}"
                )
                return identifier
            self._runner = self._make_env_script_runner(
                environment_script=wrap_env.script,
                os_env_vars=action_env_vars,
                symtab=hook_symtab,
                preallocated_file_records=wrap_file_records,
            )
            # Sets the subprocess running; returns once it has started, or has
            # failed to start. Set RUNNING first: wrap_env_enter() may fail
            # immediately (e.g. an embedded file that cannot be written) and
            # set the action state to FAILED itself.
            self._action_state = ActionState.RUNNING
            self._state = SessionState.RUNNING
            self._runner.wrap_env_enter()
        else:
            self._runner = self._make_env_script_runner(
                environment_script=environment.script,
                os_env_vars=action_env_vars,
                symtab=symtab,
            )
            self._action_state = ActionState.RUNNING
            self._state = SessionState.RUNNING
            self._runner.enter()

        return identifier

    def exit_environment(
        self,
        *,
        identifier: EnvironmentIdentifier,
        os_env_vars: Optional[dict[str, str]] = None,
        keep_session_running: bool = False,
        resolved_symtab: Optional["SerializedSymbolTable"] = None,
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
            resolved_symtab (Optional[SerializedSymbolTable]): See
                :meth:`enter_environment` for semantics. Pass the same table
                the environment was entered with so its onExit resolves in
                the same scope as its onEnter.

        Raises:
            RuntimeError: If the Session is not in the READY or READY_ENDING state;
                if the given identifier is not known to this Session; or if it is
                not the next Environment that must be exited.
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

        # RFC 0008: capture the openjd_env list for WrappedAction.Environment
        # _before_ the exiting environment is removed from tracking, so the
        # list includes that environment's own openjd_env variables — the
        # real subprocess environment (action_env_vars, computed above)
        # includes them, and the wrapped onExit runs with them in the
        # unwrapped case too.
        wrapped_session_env_list = self._collect_session_env_list()

        # Remove the environment from our tracking since we're now exiting it.
        del self._environments[identifier]
        self._environments_entered.pop()
        # RFC 0008: drop any embedded-file records reused across this (wrap)
        # environment's hook invocations; the files themselves live in the
        # session directory and are cleaned up with it.
        self._wrap_env_file_records.pop(identifier, None)
        # Drained here, with the rest of this environment's per-enter
        # tracking, so no failure branch below can strand it. A stranded entry
        # is not merely a leak: identifiers may be supplied by the caller and
        # reused after an exit, and a re-entered identifier would then replay
        # this stale context. The replay into the symbol table stays below,
        # where the table exists.
        exit_step_name = self._environment_step_names.pop(identifier, None)
        # Safe to drop here even though a wrap hook may intercept this exit:
        # the interceptor is always a different, still-entered outer
        # environment, and an environment that has exited can never intercept
        # again.
        self._environment_resolved_bases.pop(identifier, None)

        self._running_environment_identifier = identifier

        # Deserialize the service-resolved base table (if given) before
        # building the symbol table, so its entries seed first.
        resolved_base: Optional[dict[str, Any]] = None
        if resolved_symtab is not None:
            try:
                resolved_base = self._resolved_base_entries(resolved_symtab)
            except ValueError as e:
                # Fail the action through the normal failure path rather than
                # raising out of the public API — the environment and its
                # stored step context were already removed from tracking
                # above, matching how the _materialize_path_mapping failure
                # below leaves it.
                self._fail_action_before_start(
                    f"Failed to deserialize the resolved symbol table: {e}"
                )
                return

        symtab = self._symbol_table(environment.revision, resolved_base=resolved_base)
        try:
            self._materialize_path_mapping(environment.revision, action_env_vars, symtab)
        except RuntimeError as e:
            self._fail_action_before_start(str(e))
            return

        # Re-seed the owning step's name (Step.Name, RFC 0005 EXPR) this
        # environment was entered with so its onExit resolves in the same
        # scope as its onEnter.
        if exit_step_name is not None:
            symtab["Step.Name"] = exit_step_name

        # Note: the environment script's own EXPR `let` bindings (RFC 0005)
        # are evaluated by the script runner (after embedded-file path
        # allocation); the wrap-interception branch below evaluates them
        # itself before resolving the wrapped onExit.

        # Note: RUNNING is set below, immediately before the runner is asked to
        # start, and never before `self._runner` exists -- see the note in
        # enter_environment for why that window matters to `cancel_action()`.

        # RFC 0008: an outer environment's onWrapEnvExit intercepts an
        # inner environment's onExit. The inner environment was already
        # popped from ``_environments_entered`` above, so a stack search
        # now only turns up genuinely-outer environments.
        on_exit_action = (
            environment.script.actions.onExit if environment.script is not None else None
        )
        wrap_env = (
            self._find_wrap_environment(hook="onWrapEnvExit")
            if on_exit_action is not None
            else None
        )

        if wrap_env is not None:
            # See the onWrapEnvEnter path (_try_inject_wrapped_symbols). On
            # failure the environment was already removed from tracking
            # above, matching how a failed exit() behaves.
            hook_symtab = self._build_wrap_hook_scope(
                environment.revision, symtab, resolved_base=resolved_base
            )
            if not self._try_inject_wrapped_symbols(
                scope=EmbeddedFilesScope.ENV,
                inner_script=environment.script,
                symtab=symtab,
                inject=lambda inner_symtab: self._inject_wrapped_env_symbols(
                    hook_symtab,
                    environment,
                    on_exit_action,
                    session_env_list=wrapped_session_env_list,
                    inner_symtab=inner_symtab,
                ),
                fail_message=(
                    f"Failed to resolve the wrapped onExit action of "
                    f"{environment.name} for {wrap_env.name}'s onWrapEnvExit"
                ),
            ):
                return
            self._seed_wrap_env_scope(hook_symtab, wrap_env)
            try:
                wrap_file_records = self._get_wrap_env_file_records(wrap_env)
            except RuntimeError as e:
                self._fail_action_before_start(
                    f"Failed to allocate embedded files for {wrap_env.name}: {e}"
                )
                return
            self._runner = self._make_env_script_runner(
                environment_script=wrap_env.script,
                os_env_vars=action_env_vars,
                symtab=hook_symtab,
                preallocated_file_records=wrap_file_records,
            )
            # Set RUNNING first: wrap_env_exit() may fail immediately and set
            # the action state to FAILED itself.
            self._action_state = ActionState.RUNNING
            self._state = SessionState.RUNNING
            self._runner.wrap_env_exit()
        else:
            self._runner = self._make_env_script_runner(
                environment_script=environment.script,
                os_env_vars=action_env_vars,
                symtab=symtab,
            )
            self._action_state = ActionState.RUNNING
            self._state = SessionState.RUNNING
            self._runner.exit()

    def run_task(
        self,
        *,
        step_script: StepScriptModel,
        task_parameter_values: TaskParameterSet,
        os_env_vars: Optional[dict[str, str]] = None,
        log_task_banner: bool = True,
        step_name: Optional[str] = None,
        resolved_symtab: Optional["SerializedSymbolTable"] = None,
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
            step_name (Optional[str]): The name of the step whose task is being run.
                Used by RFC 0008 to populate ``WrappedStep.Name`` in wrap hooks.
                Required when a wrap Environment is active.
            resolved_symtab (Optional[SerializedSymbolTable]): The step-scope
                symbol table generated by ``create_job`` (available as
                ``Step.resolved_symtab``). It contains ``Param.*``,
                ``RawParam.*``, ``Job.Name``, ``Step.Name``, and the
                step-level let-binding values. Its entries seed the session
                symbol table first; ``Session.*`` and ``Task.*`` values layer
                on top to evaluate the script-level let bindings and the
                action arguments — the layering the openjd-rs runtime applies
                to the same table. ``None`` is fine when the script has no
                let bindings and no expression interpolation that depends on
                step-scope state.

        Raises:
            RuntimeError: If the Session is not in the READY state.
            ValueError: If a wrap Environment (RFC 0008) is active and no
                ``step_name`` was given.
        """
        if self.state != SessionState.READY:
            raise RuntimeError("Session must be in the READY state to run a task.")

        # Look up the active wrap environment (RFC 0008) up front, before any
        # state is reset or anything is logged, so that rejecting the call leaves
        # the session exactly as it was — including the previous action's status.
        wrap_env = self._find_wrap_environment(hook="onWrapTaskRun")
        if wrap_env is not None and step_name is None:
            # RFC 0008 defines WrappedStep.Name as the name of the wrapped step,
            # and <StepName> has a minimum length of one — there is no "unknown
            # step" value to render. Rendering the empty string instead would
            # silently hand a wrap script an empty container or label name, so
            # this is reported as what it is: caller misuse, in the same shape
            # run_task already reports caller misuse. Raising (rather than
            # failing the action) keeps the session usable, so the caller can
            # retry with a step name.
            raise ValueError(
                f"run_task() requires step_name when a wrap environment "
                f"('{wrap_env.name}') is active: RFC 0008's WrappedStep.Name "
                f"has no value to render without it."
            )

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
        # Deserialize the service-resolved base table (if given) before
        # building the symbol table, so its entries seed first.
        resolved_base: Optional[dict[str, Any]] = None
        if resolved_symtab is not None:
            try:
                resolved_base = self._resolved_base_entries(resolved_symtab)
            except ValueError as e:
                # Fail the action through the normal failure path rather than
                # raising out of the public API.
                self._fail_action_before_start(
                    f"Failed to deserialize the resolved symbol table: {e}"
                )
                return
        symtab = self._symbol_table(
            step_script.revision, task_parameter_values, resolved_base=resolved_base
        )
        # RFC 0005; Template Schemas §7.3.1 (EXPR): the running step's name. Only EXPR templates
        # pass validation referencing Step.Name, so seeding it when known does
        # not change non-EXPR behavior.
        if step_name is not None:
            symtab["Step.Name"] = step_name

        action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)
        try:
            self._materialize_path_mapping(step_script.revision, action_env_vars, symtab)
        except RuntimeError as e:
            self._fail_action_before_start(str(e))
            return

        # Note: the step script's EXPR `let` bindings (RFC 0005) are evaluated
        # by the script runner, after embedded-file paths are allocated (so
        # bindings can reference Task.File.*). The wrap-interception branch
        # below evaluates them itself before resolving the wrapped onRun.

        # If a wrap environment is active, inject WrappedAction.* into the symbol
        # table and run its hook instead of the step script's onRun (RFC 0008).
        if wrap_env is not None:
            # RFC 0008 requires WrappedStep.Name, and a wrapped run without a step
            # name is already rejected at the top of this method, under this same
            # `wrap_env is not None` condition. So this is narrowing for the type
            # checker, not a runtime invariant, and a second runtime check here
            # would be unreachable -- CodeQL says so, and it is right. Bound once,
            # where the requirement has been proven, rather than re-checked.
            wrapped_step_name = cast(str, step_name)
            # Two separate scopes. The wrapped onRun resolves against the STEP's
            # own scope (`symtab`, which carries this task's parameters and the
            # running step's name); the hook resolves against its own table,
            # which deliberately carries none of that -- see
            # _build_wrap_hook_scope. The wrap environment's own lets/files are
            # evaluated into the hook's table by the script runner from
            # wrap_env.script.
            hook_symtab = self._build_wrap_hook_scope(
                step_script.revision, symtab, resolved_base=resolved_base
            )
            if not self._try_inject_wrapped_symbols(
                scope=EmbeddedFilesScope.STEP,
                inner_script=step_script,
                symtab=symtab,
                inject=lambda inner_symtab: self._inject_wrapped_task_symbols(
                    hook_symtab, step_script, wrapped_step_name, inner_symtab=inner_symtab
                ),
                fail_message=(
                    f"Failed to resolve the wrapped Task action for {wrap_env.name}'s "
                    "onWrapTaskRun"
                ),
            ):
                return

            # Give the hook the step context its OWN environment was entered
            # with. Ordering is no longer load-bearing here: `hook_symtab` is a
            # separate table and is never the base of
            # `_build_wrapped_inner_scope`, so seeding it cannot reach the
            # wrapped action's scope whenever it happens. It stays after
            # injection to keep all three hook paths reading the same way.
            self._seed_wrap_env_scope(hook_symtab, wrap_env)

            try:
                wrap_file_records = self._get_wrap_env_file_records(wrap_env)
            except RuntimeError as e:
                self._fail_action_before_start(
                    f"Failed to allocate embedded files for {wrap_env.name}: {e}"
                )
                return
            self._runner = self._make_env_script_runner(
                environment_script=wrap_env.script,
                os_env_vars=action_env_vars,
                symtab=hook_symtab,
                preallocated_file_records=wrap_file_records,
            )
            # Note: unlike enter_environment()/exit_environment(), which set
            # RUNNING before their first failable step, this path sets it only
            # after wrapped-symbol injection has succeeded — so an injection
            # failure reports FAILED/READY_ENDING without the session ever
            # having been observably RUNNING. Harmless for the documented
            # poll-then-check-action_status pattern, but the asymmetry is
            # deliberate: there is no runner to own the failure until here.
            self._action_state = ActionState.RUNNING
            self._state = SessionState.RUNNING
            self._runner.wrap_task_run()
        else:
            # Original path: run the step script directly.
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

    def _run_task_without_session_env(
        self,
        *,
        step_script: StepScriptModel,
        task_parameter_values: TaskParameterSet,
        os_env_vars: Optional[dict[str, str]] = None,
        log_task_banner: bool = True,
    ) -> None:
        """Private API to run a task within the session.
        This method directly use os_env_vars passed in without applying additional session env setup.

        Note: unlike :meth:`run_task`, this deliberately does *not* dispatch RFC
        0008 wrap hooks, and does not seed ``Step.Name``. A caller that uses this
        while a wrap Environment is entered runs the task unwrapped. Callers must
        move to :meth:`run_task` to participate in wrap actions.
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

        # Evaluate environment variables
        action_env_vars = dict[str, Optional[str]](self._process_env)  # Make a copy
        if os_env_vars:
            action_env_vars.update(**os_env_vars)

        try:
            self._materialize_path_mapping(step_script.revision, action_env_vars, symtab)
        except RuntimeError as e:
            self._fail_action_before_start(str(e))
            return

        # Note: the step script's EXPR `let` bindings (RFC 0005) are evaluated
        # by the script runner, after embedded-file paths are allocated.

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

    def run_subprocess(
        self,
        *,
        command: str,
        args: Optional[list[str]] = None,
        timeout: Optional[int] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        use_session_env_vars: bool = True,
        log_banner_message: Optional[str] = None,
    ) -> None:
        """Run an ad-hoc subprocess within the Session.

        This method is non-blocking; it will exit when the subprocess is either
        confirmed to have started running, or has failed to be started.

        Arguments:
            command (str): The command/executable to run. Used exactly as provided
                without format string substitution.
            args (Optional[list[str]]): Arguments to pass to the command. Used exactly
                as provided without format string substitution. Defaults to None.
            timeout (Optional[int]): Maximum allowed runtime of the subprocess in seconds.
                Must be a positive integer if provided. If None, the subprocess can run
                indefinitely. Defaults to None.
            os_env_vars (Optional[dict[str, str]]): Additional OS environment variables
                to inject into the subprocess. Values provided override original process
                environment variables and are overridden by environment-defined variables.
            use_session_env_vars (bool): If True, includes environment variables from
                the session and entered environments. If False, only uses os_env_vars
                and original process environment variables. Defaults to True.
            log_banner_message (Optional[str]): Custom message to display in a banner
                before running the subprocess. If provided, logs a banner with this message.
                If None, no banner is logged. Defaults to None.

        Raises:
            RuntimeError: If the Session is not in the READY state.
            ValueError: If timeout is provided and is not a positive integer, or if command is empty.
        """
        # State validation
        if self.state != SessionState.READY:
            raise RuntimeError(
                f"Session must be in the READY state to run a subprocess. "
                f"Current state: {self.state.value}"
            )

        # Parameter validation
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be a positive integer")

        if not command or not command.strip():
            raise ValueError("command must be a non-empty string")

        # Log banner if requested
        if log_banner_message:
            log_section_banner(self._logger, log_banner_message)

        # Reset action state
        self._reset_action_state()

        # Construct Action model
        cancelation = CancelationMethodTerminate_2023_09(mode=CancelationMode_2023_09.TERMINATE)

        action_command = CommandString_2023_09(command)
        action_args = [ArgString_2023_09(arg) for arg in args] if args else None

        action = Action_2023_09(
            command=action_command,
            args=action_args,
            timeout=timeout,
            cancelation=cancelation,
        )

        # Construct StepScript model
        step_actions = StepActions_2023_09(onRun=action)

        step_script = StepScript_2023_09(
            actions=step_actions,
            embeddedFiles=None,
        )

        # Create empty symbol table (no format string substitution for ad-hoc subprocesses)
        symtab = SymbolTable()

        # Evaluate environment variables
        if use_session_env_vars:
            action_env_vars = self._evaluate_current_session_env_vars(os_env_vars)
        else:
            action_env_vars = dict[str, Optional[str]](self._process_env)  # Make a copy
            if os_env_vars:
                action_env_vars.update(**os_env_vars)

        # Note: Path mapping is not materialized for ad-hoc subprocesses since it's only
        # accessible via template variable substitution (e.g., {{Session.PathMappingRulesFile}}),
        # which is explicitly disabled for run_subprocess to ensure predictable behavior.

        # Create and start StepScriptRunner
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
        resolved_base: Optional[dict[str, Any]] = None,
    ) -> SymbolTable:
        """Construct a SymbolTable, with fully qualified value names, suitable for running a Script.

        ``resolved_base`` (from :meth:`_resolved_base_entries`) seeds the
        table before every session-derived value, so the latter layer on
        top — the ordering the openjd-rs runtime applies to its base table.
        """

        def apply_mapping(path: str) -> str:
            if self._path_mapping_rules is not None:
                # Apply path mapping rules in the order given until one does a replacement
                for rule in self._path_mapping_rules:
                    changed, result = rule.apply(path=path)
                    if changed:
                        return result
            return path

        def processed_parameter_value(param: ParameterValue) -> Any:
            if param.type == ParameterValueType.PATH:
                return apply_mapping(param.value)
            if param.type == ParameterValueType.LIST_PATH and isinstance(param.value, list):
                # openjd-rs maps each element of a LIST[PATH] parameter at
                # session scope; mirror that element-wise mapping.
                return [apply_mapping(p) for p in param.value]
            return param.value

        def record_expr_types(
            expr_types: dict[str, str], key: str, raw_key: str, ptype: ParameterValueType
        ) -> None:
            """Record the EXPR types for a parameter's ``Param.*``/``RawParam.*``
            (or ``Task.Param.*``/``Task.RawParam.*``) symbols, mirroring the
            openjd-rs session-scope symbol table:

            - ``Param.*`` carries the declared type (a PATH is a host-format
              path value with path mapping applied).
            - ``RawParam.*`` carries the raw string form: PATH stays a plain
              string and LIST[PATH] is a LIST[STRING].
            - CHUNK[INT] range strings stay strings (engine inference).

            The types only affect EXPR-extension expression evaluation; the
            legacy (non-EXPR) interpolation path ignores them.
            """
            if ptype == ParameterValueType.CHUNK_INT:
                return
            expr_types[key] = ptype.value
            if ptype == ParameterValueType.PATH:
                pass  # raw form stays an (inferred) plain string
            elif ptype == ParameterValueType.LIST_PATH:
                expr_types[raw_key] = ParameterValueType.LIST_STRING.value
            else:
                expr_types[raw_key] = ptype.value

        if version == SpecificationRevision.v2023_09:
            symtab = SymbolTable()
            # The session is host scope: enable EXPR host-context functions
            # (e.g. apply_path_mapping) with this session's rules.
            symtab.expr_host_rules = self._expr_host_rules
            # Seed the service-resolved base first, so every session-derived
            # value below overwrites it: Session.WorkingDirectory, the
            # path-mapped Param.* values, and Task.* all layer on top,
            # matching the openjd-rs runtime's layering over the same table.
            # A base entry for a name the session does not know survives.
            if resolved_base:
                for base_name, base_value in resolved_base.items():
                    symtab[base_name] = base_value
            working_dir_key = ValueReferenceConstants_2023_09.WORKING_DIRECTORY.value
            symtab[working_dir_key] = str(self.working_directory)
            # Session.WorkingDirectory is a host-format path value in openjd-rs.
            symtab.expr_types[working_dir_key] = ParameterValueType.PATH.value
            # RFC 0005; Template Schemas §7.3.1 (EXPR): the job's resolved name. Only templates
            # declaring EXPR pass validation referencing Job.Name, so seeding
            # it whenever known does not change non-EXPR behavior. A base
            # entry wins over the constructor value: in openjd-rs, Job.Name
            # rides the base and is never re-set, and both values come from
            # the service anyway.
            if self._job_name is not None and "Job.Name" not in symtab:
                symtab["Job.Name"] = self._job_name
            for param_name, param_props in self._job_parameter_values.items():
                raw_key = (
                    f"{ValueReferenceConstants_2023_09.JOB_PARAMETER_RAWPREFIX.value}.{param_name}"
                )
                key = f"{ValueReferenceConstants_2023_09.JOB_PARAMETER_PREFIX.value}.{param_name}"
                symtab[raw_key] = param_props.value
                symtab[key] = processed_parameter_value(param_props)
                record_expr_types(symtab.expr_types, key, raw_key, param_props.type)
            if task_parameter_values:
                for param_name, param_props in task_parameter_values.items():
                    raw_key = f"{ValueReferenceConstants_2023_09.TASK_PARAMETER_RAWPREFIX.value}.{param_name}"
                    key = f"{ValueReferenceConstants_2023_09.TASK_PARAMETER_PREFIX.value}.{param_name}"
                    symtab[raw_key] = param_props.value
                    symtab[key] = processed_parameter_value(param_props)
                    record_expr_types(symtab.expr_types, key, raw_key, param_props.type)
            return symtab
        else:
            raise NotImplementedError(f"Schema version {str(version.value)} is not supported.")

    def _resolved_base_entries(self, resolved_symtab: "SerializedSymbolTable") -> dict[str, Any]:
        """Deserialize a service-resolved symbol table (``Step.resolved_symtab``
        from ``create_job``) into its flat ``{name: ExprValue}`` entries, in
        host path format, ready to seed :meth:`_symbol_table`.

        Raises:
            ValueError: An entry failed validation
                (``SerializedSymbolTable.to_symtab`` validates entry contents
                lazily; ``from_json_str`` only checked JSON well-formedness).
        """
        # Guarded runtime import (extension purity): the caller already holds
        # a native SerializedSymbolTable, so the engine extension is
        # necessarily loaded — reaching this import cannot be what loads it.
        # Do not move it to module level; see
        # test/openjd/test_import_purity.py.
        from openjd.expr import PathFormat

        # The Python engine bindings have no PathFormat.host(); derive it.
        host_format = PathFormat.WINDOWS if os.name == "nt" else PathFormat.POSIX
        engine_tab = resolved_symtab.to_symtab(path_format=host_format)
        # `symbols` is the set of flat dotted leaf names; indexing returns
        # the engine's typed ExprValue. (`keys` is top-level only — not what
        # is needed here.)
        return {name: engine_tab[name] for name in engine_tab.symbols}

    def _build_expr_host_rules(self) -> Optional[list[Any]]:
        """Convert this session's path mapping rules to their engine
        (``openjd.expr.PathMappingRule``) form for EXPR host-context
        evaluation. Returns an empty list when the session has no rules (the
        session is still host scope), or ``None`` when the engine bindings
        are unavailable (pre-EXPR openjd-model)."""
        if not self._path_mapping_rules:
            # Nothing to convert, so no engine objects are needed and the import
            # below is pure cost. Returning the empty list here is exactly what
            # the loop produced anyway, and it keeps a session that never
            # evaluates an EXPR expression from loading the native extension at
            # all -- ``import openjd.expr`` is a facade over
            # ``openjd._openjd_rs``, and ``__init__`` calls this
            # unconditionally, so without this the deferral won by the
            # module-level import removal was only from import time to
            # first-session time.
            #
            # ``[]`` and not ``None``: the session is still host scope, so
            # ``apply_path_mapping()`` must remain available with an empty rule
            # set. ``SymbolTable.expr_host_rules`` stores this untouched
            # (``Optional[list[Any]]``) and only crosses into Rust at evaluation
            # time, so seeding ``[]`` costs nothing here.
            return []
        try:
            from openjd.expr import PathFormat as ExprPathFormat
            from openjd.expr import PathMappingRule as ExprPathMappingRule
        except ImportError:
            return None
        rules = []
        for rule in self._path_mapping_rules or []:
            rules.append(
                ExprPathMappingRule(
                    source_path_format=getattr(ExprPathFormat, rule.source_path_format.name),
                    source_path=str(rule.source_path),
                    destination_path=str(rule.destination_path),
                )
            )
        return rules

    # ------------------------------------------------------------------
    # RFC 0008 wrap-action helpers
    # ------------------------------------------------------------------

    def _find_wrap_environment(self, *, hook: str) -> Optional[EnvironmentModel]:
        """Walk the environment stack (innermost first) and return the
        active wrapping environment for ``hook`` (one of ``onWrapEnvEnter``,
        ``onWrapTaskRun``, or ``onWrapEnvExit``).

        Per RFC 0008 the session is only valid with at most one
        wrap-defining environment in the stack, so the first match is
        always the one that applies."""
        if hook not in WRAP_HOOK_ACTION_NAMES:
            raise ValueError(f"Unknown wrap hook name: {hook}")
        for env_id in reversed(self._environments_entered):
            env = self._environments[env_id]
            if (
                env.script is not None
                and hasattr(env.script.actions, hook)
                and getattr(env.script.actions, hook) is not None
            ):
                return env
        return None

    def _wrap_env_identifier(self, wrap_env: EnvironmentModel) -> EnvironmentIdentifier:
        """The entered-stack identifier of ``wrap_env``.

        Raises:
            RuntimeError: if the environment is not in the entered stack.
        """
        identifier = next(
            (
                env_id
                for env_id in self._environments_entered
                if self._environments[env_id] is wrap_env
            ),
            None,
        )
        if identifier is None:  # pragma: no cover - guarded by _find_wrap_environment
            raise RuntimeError(
                f"Wrap environment '{wrap_env.name}' is not in this Session's entered stack."
            )
        return identifier

    def _build_wrap_hook_scope(
        self,
        version: SpecificationRevision,
        session_symtab: SymbolTable,
        resolved_base: Optional[dict[str, Any]] = None,
    ) -> SymbolTable:
        """The scope an RFC 0008 wrap hook resolves in.

        Built fresh from session scope rather than derived from the inner
        entity's table, and that distinction is the whole point. The inner
        entity's table carries symbols belonging to the wrapped work: a task's
        ``Task.Param.*``/``Task.RawParam.*``, the running step's ``Step.Name``,
        and the resolved base the *inner* environment was entered with.
        A wrap environment must not be able to read any of them.

        :meth:`_build_wrapped_inner_scope` already blocks the wrap -> inner
        direction by resolving the wrapped action against a copy. This is the
        other direction, which was open: the hook used to resolve against the
        inner entity's own table, so ``{{Task.Param.Frame}}`` in a hook's args
        resolved to the wrapped task's frame number and ``{{Step.Name}}`` to the
        running step. RFC 0008 supplies ``WrappedStep.Name`` precisely because
        ``Step.Name`` is not meant to be reachable from a hook, and the model
        does not reject either reference in an environment script, so this was
        the only gate.

        What a hook legitimately gets: session scope (``Session.*``,
        ``Job.Name``, ``Param.*``/``RawParam.*``), the service-resolved base
        ``resolved_base`` seeds, the path-mapping symbols, the wrap
        environment's *own* enter-time step context (applied afterwards by
        :meth:`_seed_wrap_env_scope`), the ``WrappedAction.*`` overlay, and its
        own script-level ``let`` bindings and embedded files (evaluated by the
        runner).

        ``resolved_base`` is the base the *inner* entity's action was handed,
        which is what openjd-rs's hook scope carries: a hook there resolves
        against the current action's full symbol table, base included. Seeding
        it does not weaken the isolation above, because the service copies only
        ``Param.*``/``RawParam.*``/``Job.Name``/``Step.Name`` and step-level
        ``let`` values into that base, never ``Task.Param.*``. With no base the
        scope is unchanged. Base ``Step.Name`` is hook-visible as a result,
        matching openjd-rs.

        The path-mapping symbols are copied rather than re-materialized: the
        rules file has already been written for this action, and both scopes must
        name the same file.
        """
        hook_symtab = self._symbol_table(version, resolved_base=resolved_base)
        for key in (
            ValueReferenceConstants_2023_09.HAS_PATH_MAPPING_RULES.value,
            ValueReferenceConstants_2023_09.PATH_MAPPING_RULES_FILE.value,
        ):
            # _materialize_path_mapping sets the value and its EXPR type
            # together, so one membership check covers both. The check itself is
            # not decoration: this method is also called on a table that has not
            # been through path mapping.
            if key in session_symtab:
                hook_symtab[key] = session_symtab[key]
                hook_symtab.expr_types[key] = session_symtab.expr_types[key]
        return hook_symtab

    def _seed_wrap_env_scope(self, symtab: SymbolTable, wrap_env: EnvironmentModel) -> None:
        """Re-seed the scope a wrap hook resolves in with the context the
        wrap environment was *entered* with: its service-resolved base and
        its ``Step.Name``.

        A wrap hook resolves in the wrap environment's own scope, and in
        openjd-rs that scope is the environment's frozen enter-time resolved
        symbol table merged onto the action's table — so a step environment
        that defines wrap hooks carries the owning step's base, including the
        step-level ``let`` values the service resolved into it, through to
        every hook invocation. Python builds a fresh session-scope table per
        action, so it has to be re-applied here from what
        :meth:`enter_environment` remembered (it already keeps the step
        context to re-apply on the exit side).

        Ordering: the base seeds first, then ``Step.Name`` overwrites it,
        because a caller that passed ``step_name`` without a resolved table
        supplies it through that channel instead.

        Call this *after* the wrapped action's own scope has been built, so
        the wrap environment's context cannot reach the wrapped action's
        resolution — only the hook's.
        """
        identifier = self._wrap_env_identifier(wrap_env)
        # Values only, exactly as _symbol_table seeds a base: the EXPR types
        # ride the values the engine already built.
        for base_name, base_value in self._environment_resolved_bases.get(identifier, {}).items():
            symtab[base_name] = base_value
        step_name = self._environment_step_names.get(identifier)
        if step_name is not None:
            symtab["Step.Name"] = step_name

    def _get_wrap_env_file_records(self, wrap_env: EnvironmentModel) -> Optional[list[_FileRecord]]:
        """Return the wrap environment's embedded-file records, allocating
        their on-disk paths on first use and reusing them for every
        subsequent wrap-hook invocation (see ``_wrap_env_file_records``).
        Returns ``None`` when the wrap environment has no embedded files.

        The allocation only reserves the paths (defining the symbols into a
        throwaway table); the per-invocation symbol definitions and content
        writes happen in the runner against that invocation's symbol table.

        Raises:
            RuntimeError: if a file path could not be allocated.
        """
        if wrap_env.script is None or wrap_env.script.embeddedFiles is None:
            return None
        # The wrap env's identifier: it is in the entered stack (that's how
        # _find_wrap_environment found it).
        identifier = self._wrap_env_identifier(wrap_env)
        records = self._wrap_env_file_records.get(identifier)
        if records is None:
            file_writer = EmbeddedFiles(
                logger=self._logger,
                scope=EmbeddedFilesScope.ENV,
                session_files_directory=self.files_directory,
                user=self._user,
            )
            # Paths only: symbols are defined (and logged) per wrap-hook
            # invocation via register_file_paths, against that invocation's
            # own symbol table.
            records = file_writer.allocate_records(wrap_env.script.embeddedFiles)
            self._wrap_env_file_records[identifier] = records
        return records

    def _environment_defines_any_wrap_hook(self, env: EnvironmentModel) -> bool:
        """``True`` iff ``env``'s script declares any of the three wrap
        hooks. Used by the single-wrap-layer validation in
        :meth:`enter_environment`."""
        if env.script is None:
            return False
        return any(
            hasattr(env.script.actions, name) and getattr(env.script.actions, name) is not None
            for name in WRAP_HOOK_ACTION_NAMES
        )

    def _build_wrapped_inner_scope(
        self,
        scope: EmbeddedFilesScope,
        let_bindings: Optional[list[str]],
        embedded_files: Optional[Any],
        base: SymbolTable,
        script: Any = None,
    ) -> SymbolTable:
        """Build the scope a wrapped action would have resolved against had
        it run unwrapped: a copy of ``base`` (the session-scope table) plus
        the inner entity's embedded files and script-level ``let`` bindings,
        in the same order the runners use (allocate file paths → evaluate
        lets → write file contents, so a file's ``data`` can reference
        let-bound values and a binding can reference ``*.File.*``).

        ``WrappedAction.*`` values are resolved against this table so that
        names defined by the WRAPPING environment (its ``let`` bindings) can
        never leak into the wrapped action's resolved command/args — and,
        symmetrically, the inner entity's lets never apply to the hook's own
        resolution scope. Mirrors openjd-rs's ``build_wrapped_inner_scope``.

        ``script`` is the inner entity's script -- the model object
        ``let_bindings`` came from -- forwarded so that a wrapped *step* script's
        merged ``let`` list is split at its template-scope boundary exactly as
        the step runner splits it (see
        :func:`~._runner_base.apply_script_let_bindings`). Without it a wrapped
        action would resolve against template-scope values re-rendered in the
        host's path format, i.e. against a scope that differs from the one it
        would have had unwrapped -- which is the whole property this method
        exists to reproduce. An inner *environment* script has no such prefix and
        is unaffected.

        Raises:
            ValueError (FormatStringError/ExpressionError): a binding or file
                reference did not resolve.
            RuntimeError: an embedded file could not be written to disk.
        """
        symtab = SymbolTable(source=base)
        if embedded_files:
            file_writer = EmbeddedFiles(
                logger=self._logger,
                scope=scope,
                session_files_directory=self.files_directory,
                user=self._user,
            )
            records = file_writer.allocate_file_paths(embedded_files, symtab)
            if let_bindings:
                apply_script_let_bindings(symtab=symtab, let_bindings=let_bindings, script=script)
            file_writer.write_file_contents(records, symtab)
        elif let_bindings:
            apply_script_let_bindings(symtab=symtab, let_bindings=let_bindings, script=script)
        return symtab

    def _try_inject_wrapped_symbols(
        self,
        *,
        scope: EmbeddedFilesScope,
        inner_script: Any,
        symtab: SymbolTable,
        inject: Callable[[SymbolTable], None],
        fail_message: str,
    ) -> bool:
        """Common wrap-interception step for the three RFC 0008 hooks: build
        the wrapped (inner) entity's own resolution scope — its ``let``
        bindings and embedded files on a COPY of the session table, so the
        hook's own scope never sees them (openjd-rs #277) — and call
        ``inject`` with it to populate the ``WrappedAction.*`` symbols in
        ``symtab``, the hook's scope.

        On failure (e.g. a binding or embedded file did not resolve or
        write, or a FEATURE_BUNDLE_1 timeout/notifyPeriod format string did
        not resolve to an integer) the action fails through the normal
        failure path (:meth:`_fail_action_before_start` with
        ``fail_message``) rather than raising out of the public API, and
        ``False`` is returned so the caller can bail out."""
        try:
            inner_symtab = self._build_wrapped_inner_scope(
                scope,
                inner_script.let if inner_script is not None else None,
                inner_script.embeddedFiles if inner_script is not None else None,
                symtab,
                inner_script,
            )
            inject(inner_symtab)
        except (FormatStringError, ValueError, RuntimeError) as e:
            self._fail_action_before_start(f"{fail_message}: {e}")
            return False
        return True

    def _collect_session_env_list(self) -> list[str]:
        """The session-defined variables as ``["KEY=value", ...]`` for
        ``WrappedAction.Environment``.

        Session-defined variables are ``openjd_env`` stdout definitions *and*
        entered environments' declarative ``variables:`` maps (openjd-rs #277);
        host-inherited variables remain intentionally excluded per RFC 0008.

        Read from :attr:`_session_env_vars`, which is **session-lifetime**, not
        from ``_environments_entered``. This used to walk the entered stack and
        flatten each environment's changes, which meant an export vanished from
        this symbol the moment its environment exited -- a violation of RFC
        0008's MUST that every ``openjd_env`` variable from any earlier action
        in the session be included. The child *process* environment is the view
        that correctly shrinks on exit; see ``_evaluate_current_session_env_vars``.

        ``_session_env_vars`` is insertion-ordered and already holds the
        effective value per name -- a later set overwrites in place, and an
        explicit ``openjd_unset_env`` removes the name -- so this is a
        formatting step only."""
        return [f"{name}={value}" for name, value in self._session_env_vars.items()]

    def _resolve_action_timeout(self, action: Any, symtab: SymbolTable) -> Optional[int]:
        """Return the wrapped action's timeout as an int (seconds), or
        ``None`` if none was specified — ``WrappedAction.Timeout`` is typed
        ``int?`` (RFC 0008), following the EXPR semantics for optional
        data, so whole-field forwarding
        (``timeout: "{{WrappedAction.Timeout}}"``) drops the field when the
        wrapped action has no timeout.

        A resolved format-string value must be a positive integer, matching
        the openjd-rs runtime and the enforcement path in
        :meth:`ScriptRunnerBase._run_action` (a non-positive value raises
        ``ValueError``, failing the action through the caller's normal
        failure path)."""
        return resolve_optional_int_field(action.timeout, symtab, ge=1, description="timeout")

    def _inject_wrapped_cancelation_symbols(
        self, symtab: SymbolTable, action: Any, *, is_task_run: bool, inner_symtab: SymbolTable
    ) -> None:
        """Populate ``WrappedAction.Cancelation.Mode`` and
        ``WrappedAction.Cancelation.NotifyPeriodInSeconds`` from the wrapped
        action's ``<Cancelation>`` (RFC 0008 follow-up,
        openjd-specifications#148).

        ``Mode`` is typed ``string?``: ``"TERMINATE"``,
        ``"NOTIFY_THEN_TERMINATE"``, or ``None`` (rendering as
        ``null``/empty in format strings) when the wrapped action defines
        no ``<Cancelation>`` — the null case is deliberately distinct from
        an explicit ``TERMINATE`` so wrap scripts can tell "author declared
        TERMINATE" apart from "author declared nothing".

        ``NotifyPeriodInSeconds`` is typed ``int?``: the effective grace
        period when the mode is ``NOTIFY_THEN_TERMINATE``, applying the
        Template Schemas 5.3.2 defaults (120 seconds for a task's ``onRun``,
        30 otherwise) when the wrapped action omits the field — i.e. the
        value the runtime would have enforced in the unwrapped case. It is
        ``None`` (rendering as ``null``/empty in format strings) when the
        mode is ``TERMINATE`` or no ``<Cancelation>`` is defined, so "not
        applicable" is not conflated with a zero-length notify period.
        """
        # Resolution goes through the same helper the enforcement path uses
        # (resolve_effective_cancelation) — including a wrapped action
        # whose own mode is deferred (a format string) — so the value a
        # wrap script sees is always the value the runtime would enforce.
        # Direct attribute access, not a getattr default: a model rename must fail
        # loudly rather than silently tell every wrap script that the wrapped
        # action declared no cancelation. See resolve_effective_cancelation.
        mode, notify_period = resolve_effective_cancelation(action.cancelation, inner_symtab)
        if mode == CancelationMode_2023_09.NOTIFY_THEN_TERMINATE.value and notify_period is None:
            notify_period = (
                TASK_RUN_DEFAULT_NOTIFY_PERIOD_SECONDS
                if is_task_run
                else ENV_ACTION_DEFAULT_NOTIFY_PERIOD_SECONDS
            )
        symtab["WrappedAction.Cancelation.Mode"] = mode
        symtab["WrappedAction.Cancelation.NotifyPeriodInSeconds"] = notify_period

    def _inject_wrapped_env_symbols(
        self,
        symtab: SymbolTable,
        environment: EnvironmentModel,
        inner_action: Any,
        session_env_list: Optional[list[str]] = None,
        *,
        inner_symtab: SymbolTable,
    ) -> None:
        """Populate ``WrappedAction.*`` and ``WrappedEnv.Name`` for
        ``onWrapEnvEnter`` / ``onWrapEnvExit`` scripts (RFC 0008).

        The wrapped action's command/args/timeout/cancelation resolve
        against ``inner_symtab`` — the scope the action would have used had
        it run unwrapped (the inner environment's own lets and embedded
        files) — while the results are written into ``symtab``, the hook's
        own scope. Keeping the two apart means a wrapper-defined name can
        never leak into the wrapped action's resolved values, and vice
        versa (openjd-rs #277).

        ``session_env_list`` overrides the collected session env list when
        the caller must capture it at a different point in time — the
        ``onWrapEnvExit`` path collects it before the exiting environment
        is removed from tracking, so the wrapped environment's own
        variables are included."""
        command = inner_action.command.resolve(symtab=inner_symtab)
        # RFC 0005 §1.3.2 typed argument semantics (null skip, list
        # flattening), shared with the enforcement path
        # (ScriptRunnerBase._run_action) so the hook sees exactly the
        # arguments the wrapped action would have run with unwrapped —
        # mirroring openjd-rs's seed_wrapped_action_symbols, which resolves
        # via the same resolve_action_args as the runner.
        args = resolve_action_arg_values(inner_action.args, inner_symtab)
        symtab["WrappedAction.Command"] = command
        symtab["WrappedAction.Args"] = args
        symtab["WrappedAction.Environment"] = (
            session_env_list if session_env_list is not None else self._collect_session_env_list()
        )
        symtab["WrappedAction.Timeout"] = self._resolve_action_timeout(inner_action, inner_symtab)
        self._inject_wrapped_cancelation_symbols(
            symtab, inner_action, is_task_run=False, inner_symtab=inner_symtab
        )
        symtab["WrappedEnv.Name"] = environment.name

    def _inject_wrapped_task_symbols(
        self,
        symtab: SymbolTable,
        step_script: StepScriptModel,
        step_name: str,
        *,
        inner_symtab: SymbolTable,
    ) -> None:
        """Populate ``WrappedAction.*`` and ``WrappedStep.Name`` for
        ``onWrapTaskRun`` (RFC 0008).

        Resolves the step script's ``onRun`` command and args format
        strings against ``inner_symtab`` — the step's own scope (its lets
        and embedded files), see ``_inject_wrapped_env_symbols`` — producing
        concrete values the wrap action can safely shell-quote with
        ``repr_sh()``. ``WrappedAction.Args`` is stored as a native
        ``list[str]`` so that ``repr_sh(WrappedAction.Args)`` quotes each
        element individually."""
        assert isinstance(step_script, StepScript_2023_09)
        on_run = step_script.actions.onRun

        symtab["WrappedAction.Command"] = on_run.command.resolve(symtab=inner_symtab)
        # RFC 0005 §1.3.2 typed argument semantics (null skip, list
        # flattening), shared with the enforcement path — see
        # _inject_wrapped_env_symbols.
        symtab["WrappedAction.Args"] = resolve_action_arg_values(on_run.args, inner_symtab)
        symtab["WrappedAction.Environment"] = self._collect_session_env_list()
        symtab["WrappedAction.Timeout"] = self._resolve_action_timeout(on_run, inner_symtab)
        self._inject_wrapped_cancelation_symbols(
            symtab, on_run, is_task_run=True, inner_symtab=inner_symtab
        )
        symtab["WrappedStep.Name"] = step_name

    def _openjd_session_root_dir(self) -> Path:
        """
        Returns (and creates if necessary) the top-level directory where Open Job Description step session
        directories are kept
        """
        if self._session_root_directory is not None:
            return self._session_root_directory

        # custom_gettempdir() owns this directory's lifecycle end to end: it
        # creates it, refuses to return a path that is a link or that another user
        # owns (_validate_temp_dir_ownership), and guarantees
        # OPENJD_TEMPDIR_MODE. The mode matters and is not merely cosmetic -- if
        # the root lacked group/other search permission we would be unable to
        # reach files under it when this process' default group is the group
        # shared with a job user, because group permissions override world
        # permissions for a member of the group.
        #
        # This used to re-apply the mode itself, with the constant duplicated. The
        # duplicate is gone rather than merely single-sourced: two creators of one
        # shared path is what let an unvalidated one hand out a directory the
        # other then chmod'ed.
        #
        # Raises: RuntimeError
        return Path(custom_gettempdir(self._logger))

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

        # prefix="" is deliberate, and is not the same as omitting it: mkdtemp()
        # substitutes its "tmp" template for a prefix of None, costing 3 characters.
        # See SESSION_DIR_NAME_LENGTH.
        #
        # Raises: RuntimeError
        return TempDir(
            dir=root_dir,
            prefix="",
            user=self._user,
            logger=self._logger,
        )

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
        # RFC 0005; Template Schemas §7.3: for EXPR evaluation Session.HasPathMappingRules is a
        # boolean and Session.PathMappingRulesFile is a path, matching
        # openjd-rs's typed session symbols. The legacy (non-EXPR)
        # interpolation path ignores these types and keeps the string forms.
        symtab.expr_types[ValueReferenceConstants_2023_09.HAS_PATH_MAPPING_RULES.value] = (
            ParameterValueType.BOOL.value
        )
        rules_json = json.dumps(rules_dict)
        # An OSError here used to escape run_task/enter_environment/exit_environment
        # with no callback and no ActionStatus, because all three call this before
        # their runner exists. openjd-rs propagates the same failure as
        # SessionError::WorkingDirectory and every call site maps it to
        # fail_action_setup(), which publishes FAILED and notifies -- so translate
        # to a RuntimeError the callers already turn into that terminal status.
        try:
            file_handle, filename = mkstemp(dir=self.working_directory, suffix=".json", text=True)
            os.close(file_handle)
            write_file_for_user(Path(filename), rules_json, self._user)
        except Exception as err:
            raise RuntimeError(
                f"Could not write the path mapping rules file in {self.working_directory}: {err}"
            ) from err
        symtab[ValueReferenceConstants_2023_09.PATH_MAPPING_RULES_FILE.value] = str(filename)
        symtab.expr_types[ValueReferenceConstants_2023_09.PATH_MAPPING_RULES_FILE.value] = (
            ParameterValueType.PATH.value
        )

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
                self._cancel_running_action_as_failed()

        elif kind == ActionMessageKind.ENV:
            if self._running_environment_identifier is None:
                # Ignore the message if we're not running an environment.
                #
                # Per How-Jobs-Are-Run, `openjd_env` "can only be emitted by the
                # Action for entering an Environment" — so a task's onRun cannot
                # define a session variable, and neither can an RFC 0008
                # onWrapTaskRun hook, which stands in for one. That keeps
                # wrapping transparent: a task that prints an `openjd_env:` line
                # behaves the same wrapped and unwrapped.
                #
                # This is deliberate, not an oversight, and it is a known
                # divergence from openjd-rs, which records such a variable in the
                # map that feeds WrappedAction.Environment while still not
                # applying it to any subprocess environment — advertising a
                # variable the wrapped context does not have. The spec does not
                # settle the case (it also says a wrap script MAY emit these
                # macros directly) and no conformance fixture covers it; filed
                # upstream. Logged at debug so an author chasing a silent no-op
                # has something to find, without adding noise to every task log.
                self._log_discarded_env_macro(kind, value)
                return
            if cancel_action_mark_failed:
                # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
                assert isinstance(value, str)

                # Cancel the action and pass the failure message
                self._cancel_running_action_as_failed()
                self._action_fail_message = value

                return
            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, dict)
            # value = { "name": <name>, "value": <value> }
            env_vars = self._created_env_vars[self._running_environment_identifier]
            env_vars.simplify_ordered_changes(
                changes=[EnvironmentVariableSetChange(name=value["name"], value=value["value"])]
            )
            # Session-lifetime copy for WrappedAction.Environment; see
            # _session_env_vars. Runs on the LoggingSubprocess IO thread, like
            # the line above -- a plain dict assignment, so no new hazard.
            self._session_env_vars[value["name"]] = value["value"]
            return
        elif kind == ActionMessageKind.UNSET_ENV:
            if self._running_environment_identifier is None:
                # Ignore the message if we're not running an environment.
                # See the ENV branch above for why this is deliberate.
                self._log_discarded_env_macro(kind, value)
                return

            if cancel_action_mark_failed:
                # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
                assert isinstance(value, str)

                # Cancel the action and pass the failure message
                self._cancel_running_action_as_failed()
                self._action_fail_message = value

                return

            # Assert for the type checker; the type is guaranteed by the ActionMonitoringFilter
            assert isinstance(value, str)
            env_vars = self._created_env_vars[self._running_environment_identifier]
            env_vars.simplify_ordered_changes(changes=[EnvironmentVariableUnsetChange(name=value)])
            # An explicit unset is the one remover from the session-lifetime
            # map, matching openjd-rs. Environment exit is not.
            self._session_env_vars.pop(value, None)
            return
        else:  # ActionMessageKind.SESSION_RUNTIME_LOGLEVEL
            assert isinstance(value, int)
            self._logger.setLevel(value)
            return

        if self._callback:
            action_status = self.action_status
            # R5-6: a plain check, not `assert`. Every branch above sets
            # `_action_state`, so this is non-None in practice -- but it is read
            # on the stdout-forwarding thread, where an AssertionError unwinds
            # LoggingSubprocess.run() before the child is waited on. Skipping the
            # notification is strictly better than losing the process.
            if action_status is not None:
                self._callback(self._session_id, action_status)

    def _cancel_running_action_as_failed(self) -> None:
        """Cancel the running action and report it as failed, if one is running.

        This is reached from the log-forwarding thread whenever a malformed
        OpenJD stdout macro is seen. The state guard matters because that filter
        is attached to the session logger, so it also sees lines the *session
        itself* logs while no action is running — a task parameter whose name
        starts with ``openjd_env`` is enough — and ``cancel_action()`` raises
        unless an action is in flight. Without the guard that RuntimeError
        propagates out of ``logger.info`` and out of the public API.
        """
        if self.state != SessionState.RUNNING:
            return
        self.cancel_action(mark_action_failed=True)

    def _log_discarded_env_macro(self, kind: ActionMessageKind, value: Any) -> None:
        """Record, at debug level, that an environment-variable stdout macro was
        ignored because the running Action is not an Environment's entry Action.

        Debug rather than a warning on purpose: ``_reset_action_state`` clears
        the running-environment identifier for every task, and this callback
        cannot tell an RFC 0008 wrap hook from an ordinary task action — so a
        warning here would fire for every existing job whose task happens to
        print an ``openjd_env:`` line.
        """
        name = value.get("name") if isinstance(value, dict) else value
        self._logger.debug(
            "Ignoring %s for '%s': environment variables can only be defined by "
            "the Action that enters an Environment.",
            kind.name.lower(),
            name,
        )

    def _fail_action_before_start(self, message: str) -> None:
        """Mark the pending action as FAILED before any runner/subprocess
        exists (RFC 0008: e.g. when resolving the wrapped action's format
        strings for ``WrappedAction.*`` injection fails).

        Mirrors the failure branch of :meth:`_action_callback` — the
        session transitions to READY_ENDING so the caller can exit the
        entered environments — but does not require ``self._runner``.
        """
        self._logger.error(message)
        self._action_fail_message = message
        self._action_exit_code = None
        self._action_state = ActionState.FAILED
        self._state = SessionState.READY_ENDING
        if self._callback:
            action_status = self.action_status
            # R5-6: a plain check, not `assert` -- `_action_state` is assigned
            # immediately above, so None here would be an internal error, and
            # skipping the callback beats raising out of a failure path.
            if action_status is None:  # pragma: no cover - defensive
                return
            # R4-6 fix: Isolate consumer-callback exceptions. Same pattern as
            # _fail_action in ScriptRunnerBase and _on_process_exit. A consumer
            # that raises must not turn a handled resolution failure into an
            # exception escaping the public Session API.
            try:
                self._callback(self._session_id, action_status)
            except Exception as exc:
                self._logger.error(
                    f"Exception in session callback: {exc}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                    ),
                )

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
        # R5-6: a bound read with an explicit check rather than `assert`. This is
        # invoked from the runner's own completion path, so `_runner` is set --
        # but `cleanup()` clears it, and this runs on the pool worker where an
        # AssertionError reaches nobody. Under `python -O` the assert vanished and
        # the very next line became an AttributeError in the same blind spot.
        runner = self._runner
        if runner is None:  # pragma: no cover - defensive
            return

        self._action_exit_code = runner.exit_code
        self._action_state = state

        # F5 fix: Snapshot action_status BEFORE publishing READY. If we set
        # _state = READY first, another thread polling session.state could see
        # READY but action_status would still reflect the old (stale or
        # incomplete) snapshot. By snapshotting here, the callback receives the
        # definitive ActionStatus that corresponds to the terminal state.
        #
        # Note: We snapshot unconditionally (not guarded by `if self._callback`)
        # because action_status is cheap and some tests check exact callback
        # invocation patterns including the __bool__ check count.
        action_status = self.action_status

        if state != ActionState.RUNNING:
            # Decide which between-action state to enter.
            if self._ending_only or self._action_state != ActionState.SUCCESS:
                # Sessions are "brittle". If there's a Task cancel or Failure then
                # we can only exit the Session.
                self._state = SessionState.READY_ENDING
            else:
                self._state = SessionState.READY

        if self._callback and action_status is not None:
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
