# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import stat
import shlex
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Lock, Timer
from typing import Callable, Optional, Sequence, Type, cast
from types import TracebackType
from tempfile import mkstemp

from openjd.model import SymbolTable
from openjd.model import FormatStringError
from openjd.model.v2023_09 import Action as Action_2023_09
from ._embedded_files import EmbeddedFiles, EmbeddedFilesScope, write_file_for_user
from ._logging import log_subsection_banner, LoggerAdapter, LogContent, LogExtraInfo
from ._os_checker import is_posix
from ._session_user import SessionUser
from ._subprocess import LoggingSubprocess
from ._types import ActionModel, ActionState, EmbeddedFilesListType
from ._win32._locate_executable import locate_windows_executable

__all__ = (
    "ScriptRunnerState",
    "CancelMethod",
    "TerminateCancelMethod",
    "NotifyCancelMethod",
    "ScriptRunnerBase",
)


class ScriptRunnerState(str, Enum):
    """State of a ScriptRunner."""

    READY = "ready"
    """Runner is not currently running anything, and can run an Action.
    """

    RUNNING = "running"
    """Runner is actively running an Action.
    """

    CANCELING = "canceling"
    """Runner is actively in the act of canceling a running Action.
    """

    CANCELED = "canceled"
    """The Action that was run by the runner was canceled.
    """

    TIMEOUT = "timeout"
    """The action has been canceled due to reaching its runtime limit."""

    FAILED = "failed"
    """Runner is done running the subprocess, and the subprocess failed.
    """

    SUCCESS = "success"
    """Runner is done running the subprocess, and the subprocess returned success.
    """


class CancelMethod:
    pass


@dataclass(frozen=True)
class TerminateCancelMethod(CancelMethod):
    """Immediately terminate the running subprocess via SIGKILL"""

    pass


TIME_FORMAT_STR = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class NotifyCancelMethod(CancelMethod):
    """Cancelation via "notify then terminate". First send a SIGTERM,
    then wait for a delay, then send a SIGKILL if the process is still running.
    """

    terminate_delay: timedelta
    """Amount of time after a SIGTERM to wait to do the SIGKILL"""


class ScriptRunnerBase(ABC):
    """Base class for a runnable Environment or Step Script.
    Responsible for running a *single* Action, and optionally canceling it.
    """

    _logger: LoggerAdapter
    """The logger to which all messages should be sent from this and the subprocess.
    """

    _user: Optional[SessionUser]
    """The user to run the subprocess as, if given.
    Else the subprocess is run as this process' user.
    """

    _os_env_vars: Optional[dict[str, Optional[str]]]
    """OS Environment variables and their values to inject into the running subprocess.
    """

    _session_working_directory: Path
    """The temporary directory in which the Session is running.
    """

    _startup_directory: Optional[Path]
    """cwd to set for the subprocess, if it's possible to set it.
    """

    _callback: Optional[Callable[[ActionState], None]]
    """Callback to invoke when the running subprocess has exited (or failed to start).
    """

    _process: Optional[LoggingSubprocess]
    """The subprocess that this runner is running, or has most recently run.
    """

    _run_future: Optional[Future]
    """The future within which the current Action for this runner is running.
    Will be None if no Action is running.
    """

    _cancel_gracetime_timer: Optional[Timer]
    """If not None, then this is a timer that is counting down the grace time
    for a NOTIFY_THEN_TERMINATE cancelation.
    self._on_notify_period_end() will be called when the timer expires.
    """

    _cancel_gracetime_end: Optional[datetime]
    """The time at which the gracetime of a NOTIFY_THEN_TERMINATE cancelation's
    graceperiod will expire.
    """

    _canceled: bool
    """True iff the subprocess was canceled.
    """

    _executable_not_found: bool
    """True only on Windows, and only if the executable command that we were given
    could not be found before even trying to run the subprocess.
    """

    _notify_canceled_action_as_failed: bool
    """True iff the subprocess was canceled but action needs to be notified as FAILED.
    """

    _runtime_limit: Optional[Timer]
    """The Timer that will fire when the currently running Action has exhausted
    its runtime limit.
    Will be None if either no Action is running or the running Action has no time
    limit.
    """

    _runtime_limit_reached: bool
    """True if and only if the Action was terminated due to reaching its runtime limit."""

    _pool: ThreadPoolExecutor
    """Pool in which to run futures for this runner.
    """

    _lock: Lock
    """A lock that must be obtained prior to mutating/creating the subprocess
    running state of this runner.
    """

    _state_override: Optional[ScriptRunnerState]
    """An override for subclasses to use to indicate that the runner is in a specific state.
    e.g. We failed to write embedded files before even trying to run the action.
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
    ):
        """
        Arguments:
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
        """

        self._logger = logger
        self._user = user
        self._os_env_vars = os_env_vars
        self._session_working_directory = session_working_directory
        self._startup_directory = startup_directory
        self._callback = callback

        self._process = None
        self._run_future = None
        self._cancel_gracetime_timer = None
        self._cancel_gracetime_end = None
        self._canceled = False
        self._notify_canceled_action_as_failed = False
        self._runtime_limit = None
        self._runtime_limit_reached = False
        self._executable_not_found = False
        self._lock = Lock()
        # Will run at most the run futures
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._state_override = None
        self._print_section_banner = True

    # Context manager for use in our tests
    def __enter__(self) -> "ScriptRunnerBase":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        """Performs a clean shutdown on the runner. This shutsdown the internal
        ThreadPoolExectutor.
        """
        self._pool.shutdown()

    @abstractmethod
    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:  # pragma: nocover
        """Cancel the runner's running Action according to whatever method is dictated
        by the specific script being run.

        Arguments:
            time_limit (Optional[timedelta]): If provided, then the cancel must be
                completed within the given number of seconds. This is for urgent
                cancels (e.g. in response to the controlling process getting a SIGTERM).
                Note: a value of 0 turns a notify-then-terminate cancel into a terminate
        """
        raise NotImplementedError("Derived class must implement this.")

    @property
    def state(self) -> ScriptRunnerState:
        """Get the state of this runner."""
        if self._state_override is not None:
            return self._state_override
        if self._process is None:
            return ScriptRunnerState.READY
        # Check on the state of the future for done/canceled first
        # If the future is done, then we have a terminal state.
        assert self._run_future is not None  # For the type checker
        if self._run_future.done():
            if self._canceled and self._notify_canceled_action_as_failed:
                return ScriptRunnerState.FAILED
            if self._canceled and self._runtime_limit_reached:
                return ScriptRunnerState.TIMEOUT
            elif self._canceled:
                return ScriptRunnerState.CANCELED
            elif self._process.failed_to_start or self._process.exit_code != 0:
                return ScriptRunnerState.FAILED
            else:
                return ScriptRunnerState.SUCCESS
        # Future's still running, so we're CANCELING if we've been canceled
        # otherwise we're RUNNING.
        if self._canceled:
            return ScriptRunnerState.CANCELING
        # If the future's not done, then we're still running.
        return ScriptRunnerState.RUNNING

    @property
    def runtime_limit_reached(self) -> bool:
        return self._runtime_limit_reached

    @property
    def exit_code(self) -> Optional[int]:
        """Note: It *is* possible to fail without an exit code."""
        if self._process is not None:
            return self._process.exit_code
        return None

    def _run(self, args: Sequence[str], time_limit: Optional[timedelta] = None) -> None:
        with self._lock:
            if self.state != ScriptRunnerState.READY:
                raise RuntimeError("This cannot be used to run a second subprocess.")
            if is_posix():
                script = self._generate_command_shell_script(args)
                filehandle, filename = mkstemp(
                    dir=self._session_working_directory, suffix=".sh", text=True
                )
                os.close(filehandle)
                # Create the shell script, and make it runnable by the owner.
                # If user is defined, then this will make it owned by that user's group.
                write_file_for_user(
                    Path(filename),
                    script,
                    user=self._user,
                    additional_permissions=stat.S_IXUSR | stat.S_IXGRP,
                )
                self._logger.debug(
                    f"Wrote the following script to {filename}:\n{script}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.FILE_PATH | LogContent.FILE_CONTENTS
                    ),
                )
            else:
                try:
                    args = locate_windows_executable(
                        args, self._user, self._os_env_vars, str(self._session_working_directory)
                    )
                except RuntimeError as e:
                    # Make use of the action filter to surface the failure reason to
                    # the customer.
                    self._logger.info(
                        f"openjd_fail: {str(e)}",
                        extra=LogExtraInfo(openjd_log_content=LogContent.EXCEPTION_INFO),
                    )
                    self._state_override = ScriptRunnerState.FAILED
                    # We haven't started the future yet that runs the process,
                    # but the Session still needs to know that the action is over.
                    if self._callback is not None:
                        self._callback(ActionState.FAILED)
                    return

            subprocess_args = [filename] if is_posix() else args
            self._process = LoggingSubprocess(
                logger=self._logger,
                args=subprocess_args,
                user=self._user,
                os_env_vars=self._os_env_vars,
                working_dir=str(self._session_working_directory),
            )

            if time_limit:
                self._runtime_limit = Timer(time_limit.total_seconds(), self._on_timelimit)
                self._runtime_limit.start()

            if self._print_section_banner:
                log_subsection_banner(self._logger, "Phase: Running action")
            self._run_future = self._pool.submit(self._process.run)
        # Intentionally leave the lock section. If the process was *really* fast,
        # then it's possible for the future to have finished before we get to add
        # the done-callback. That results in the done-callback being called from
        # *this* thread.
        self._run_future.add_done_callback(self._on_process_exit)

        # Block until the subprocess actually starts.
        # This will prevent race conditions where the user starts up the Action,
        # and then is erroneously told that the Action is done because the future
        # for _process.run hasn't actually gotten far enough to start the subprocess
        # before we check self.state
        self._process.wait_until_started()

        if self.state == ScriptRunnerState.RUNNING and self._callback is not None:
            # Let the caller know that the process is running.
            self._callback(ActionState.RUNNING)

    def _generate_command_shell_script(self, args: Sequence[str]) -> str:
        """Generate a shell script for running a command given by the args."""
        script = list[str]()
        script.append("#!/bin/sh\n")
        if self._os_env_vars:
            for name, value in self._os_env_vars.items():
                if value is None:
                    script.append(f"unset {name}")
                else:
                    script.append(f"export {name}={shlex.quote(value)}")
        if self._startup_directory is not None:
            # Note: Single quotes around the path as it may have spaces, and we don't want to
            # process any shell commands in the path.
            script.append(f"cd '{self._startup_directory}'")
        script.append("exec " + shlex.join(args))
        return "\n".join(script)

    def _materialize_files(
        self,
        scope: EmbeddedFilesScope,
        files: EmbeddedFilesListType,
        dest_directory: Path,
        symtab: SymbolTable,
    ) -> None:
        """Helper for derived classes that wraps all of the logic around
        materializing embedded files to disk.

        """
        file_writer = EmbeddedFiles(
            logger=self._logger,
            scope=scope,
            session_files_directory=dest_directory,
            user=self._user,
        )
        try:
            file_writer.materialize(files, symtab)
        except RuntimeError as exc:
            # Had a problem writing at least one file to disk.
            # Surface the error.
            # Make use of the action filter to surface the failure reason to
            # the customer.
            self._logger.info(
                f"openjd_fail: {str(exc)}",
                extra=LogExtraInfo(openjd_log_content=LogContent.EXCEPTION_INFO),
            )
            self._state_override = ScriptRunnerState.FAILED
            # We haven't started the future yet that runs the process,
            # but the Session still needs to know that the action is over.
            if self._callback is not None:
                self._callback(ActionState.FAILED)

    def _run_action(
        self,
        action: ActionModel,
        symtab: SymbolTable,
        *,
        default_timeout: Optional[timedelta] = None,
    ) -> None:
        """Helper for derived classes to run a specific Action.

        Args:
            action (ActionModel): The action model to be executed. Must be an
                instance of Action_2023_09.
            symtab (SymbolTable): Symbol table used for resolving command and
                arguments.
            default_timeout (Optional[timedelta], optional): Default timeout duration
                for the action if no timeout is specified in the action. The default behaviour if
                None is passed will allow the action to run indefinitely until it completes.
        """
        assert isinstance(action, Action_2023_09)
        try:
            command = [action.command.resolve(symtab=symtab)]
            if action.args is not None:
                command.extend(s.resolve(symtab=symtab) for s in action.args)
        except FormatStringError as exc:
            # Extremely unlikely since a JobTemplate needs to have passed
            # validation before we could be running it, but just to be safe.
            self._logger.info(
                f"openjd_fail: {str(exc)}",
                extra=LogExtraInfo(openjd_log_content=LogContent.EXCEPTION_INFO),
            )
            self._state_override = ScriptRunnerState.FAILED
            # We haven't started the future yet that runs the process,
            # but the Session still needs to know that the action is over.
            if self._callback is not None:
                self._callback(ActionState.FAILED)
        else:
            time_limit: Optional[timedelta] = default_timeout
            if action.timeout:
                time_limit = timedelta(seconds=action.timeout)
            self._run(command, time_limit)

    def _cancel(
        self,
        method: CancelMethod,
        time_limit: Optional[timedelta] = None,
        mark_action_failed: bool = False,
    ) -> None:
        # For the type checkers
        assert self._process is not None
        # Nothing to do if it's not running.
        if not self._process.is_running:
            return

        with self._lock:
            self._canceled = True
            self._notify_canceled_action_as_failed = mark_action_failed
            now = datetime.now(timezone.utc)
            now_str = now.strftime(TIME_FORMAT_STR)
            if self._cancel_gracetime_timer is not None:
                # This cancel request is a duplicate that may have a different gracetime.
                # We'll recalculate the gracetime
                self._cancel_gracetime_timer.cancel()
                self._cancel_gracetime_timer = None

            if isinstance(method, TerminateCancelMethod):
                self._logger.info(
                    f"Canceling subprocess {str(self._process.pid)} via termination method at {now_str}.",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )
                try:
                    self._process.terminate()
                except OSError as err:  # pragma: nocover
                    # Being paranoid. Won't happen... if we could start the process, then we can send it a signal
                    self._logger.warning(
                        f"Cancelation could not send terminate signal to process {self._process.pid}: {str(err)}",
                        extra=LogExtraInfo(
                            openjd_log_content=LogContent.PROCESS_CONTROL
                            | LogContent.EXCEPTION_INFO
                        ),
                    )
            else:
                self._logger.info(
                    f"Canceling subprocess {str(self._process.pid)} via notify then terminate method at {now_str}.",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )
                method = cast(NotifyCancelMethod, method)

                gracetime = (
                    min(time_limit, method.terminate_delay)
                    if time_limit is not None
                    else method.terminate_delay
                )
                if self._cancel_gracetime_end is not None:
                    # How much time is remaining in the previous cancel?
                    time_remaining = self._cancel_gracetime_end - now
                    # Our gracetime is the minimum of remaining and the new time limit
                    gracetime = min(gracetime, time_remaining)
                self._cancel_gracetime_end = now + gracetime

                # 1) Create the notification file
                #      Note: Notify-then-terminate requires writing a "cancel_info.json" file to
                #      the session working directory. Contents are JSON formatted with contents:
                #      { "NotifyEnd": "<yyyy>-<mm>-<dd>T<hh>:<mm>:<ss>Z" }
                #      where the given time is the time at which the notify period will end (i.e. when
                #      when we'll send the SIGKILL)
                grace_end_time_str = self._cancel_gracetime_end.strftime(TIME_FORMAT_STR)
                notify_end = json.dumps({"NotifyEnd": grace_end_time_str})
                write_file_for_user(
                    self._session_working_directory / "cancel_info.json", notify_end, self._user
                )
                self._logger.info(
                    f"Grace period ends at {grace_end_time_str}",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )

                # 2) Send the notify
                try:
                    self._process.notify()
                except OSError as err:  # pragma: nocover
                    # Being paranoid. Won't happen... if we could start the process, then we can send it a signal
                    self._logger.warning(
                        f"Cancelation could not send notify signal to process {self._process.pid}: {str(err)}",
                        extra=LogExtraInfo(
                            openjd_log_content=LogContent.PROCESS_CONTROL
                            | LogContent.EXCEPTION_INFO
                        ),
                    )

                # 4) Set up the timer to send the terminate signal
                self._cancel_gracetime_timer = Timer(
                    gracetime.total_seconds(), self._on_notify_period_end
                )
                self._cancel_gracetime_timer.start()

    def _on_process_exit(self, future: Future) -> None:
        """This is invoked as a callback when run_future is done."""
        assert self._run_future is not None
        with self._lock:
            if self._runtime_limit is not None:
                self._runtime_limit.cancel()
                self._runtime_limit = None

            if self._cancel_gracetime_timer is not None:
                self._cancel_gracetime_timer.cancel()
                self._cancel_gracetime_timer = None

            if exc := self._run_future.exception():
                self._logger.error(
                    f"Error running subprocess: {str(exc)}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                    ),
                )

            if self._callback is not None:
                self._callback(ActionState(self.state.value))

    def _on_notify_period_end(self) -> None:
        """This is invoked when the grace period in a NOTIFY_THEN_TERMINATE
        cancelation has expired.
        """
        assert self._process is not None
        with self._lock:
            self._cancel_gracetime_timer = None
        self._logger.info(
            "Notify period ended. Terminate at %s",
            datetime.now(timezone.utc).strftime(TIME_FORMAT_STR),
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        try:
            self._process.terminate()
        except OSError as err:  # pragma: nocover
            # Being paranoid. Won't happen... if we could start the process, then we can send it a kill signal
            self._logger.warning(
                f"Cancelation could not send terminate signal to process {self._process.pid}: {str(err)}",
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                ),
            )

    def _on_timelimit(self) -> None:
        """Callback that is invoked when the runtime limit of the running
        process has expired.
        """
        with self._lock:
            self._runtime_limit = None
        self._logger.info(
            "TIMEOUT - Runtime limit reached at %s. Canceling action.",
            datetime.now(timezone.utc).strftime(TIME_FORMAT_STR),
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        self._runtime_limit_reached = True
        self.cancel()
