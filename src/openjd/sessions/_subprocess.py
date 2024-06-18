# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import shlex
from ._os_checker import is_posix, is_windows

if is_windows():
    from subprocess import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW  # type: ignore
    from ._win32._popen_as_user import PopenWindowsAsUser  # type: ignore
    from ._windows_process_killer import kill_windows_process_tree
from typing import Any
from threading import Event
from logging import LoggerAdapter
from subprocess import DEVNULL, PIPE, STDOUT, Popen, list2cmdline, run
from typing import Callable, Optional, Sequence, cast
from pathlib import Path
from datetime import timedelta
import sys

from ._session_user import PosixSessionUser, WindowsSessionUser, SessionUser


__all__ = ("LoggingSubprocess",)

# ========================================================================
# ========================================================================
# DEVELOPER NOTE:
#  If you make changes to this class's implementation, then be sure to test
# the cross-user functionality to make sure that is intact. The
# scripts/run_sudo_tests.sh script in this repository can be used to
# run these tests on Linux.
# ========================================================================
# ========================================================================

POSIX_SIGNAL_SUBPROC_SCRIPT_PATH = (
    Path(__file__).parent / "_scripts" / "_posix" / "_signal_subprocess.sh"
)

WINDOWS_SIGNAL_SUBPROC_SCRIPT_PATH = (
    Path(__file__).parent / "_scripts" / "_windows" / "_signal_win_subprocess.py"
)

LOG_LINE_MAX_LENGTH = 64 * 1000  # Start out with 64 KB, can increase if needed


def _exit_code_to_32bit_signed(exitcode: Optional[int]) -> Optional[int]:
    # Workaround to ensure that the process exit code is returned in range of
    # a 32-bit signed integer.
    if exitcode is None:
        return None
    as_uint32_bytes = (exitcode & 0xFFFFFFFF).to_bytes(4, "big", signed=False)
    return int.from_bytes(as_uint32_bytes, "big", signed=True)


class LoggingSubprocess(object):
    """A process whose stdout/stderr lines are sent to a given Logger."""

    _logger: LoggerAdapter
    _process: Optional[Popen]
    _args: Sequence[str]
    _encoding: str
    _user: Optional[SessionUser]
    _callback: Optional[Callable[[], None]]
    _start_failed: bool
    _has_started: Event
    _os_env_vars: Optional[dict[str, Optional[str]]]
    _working_dir: Optional[str]

    _pid: Optional[int]
    _returncode: Optional[int]

    def __init__(
        self,
        *,
        logger: LoggerAdapter,
        args: Sequence[str],
        encoding: str = "utf-8",
        user: Optional[SessionUser] = None,  # OS-user to run as
        callback: Optional[Callable[[], None]] = None,
        os_env_vars: Optional[dict[str, Optional[str]]] = None,
        working_dir: Optional[str] = None,
    ):
        if len(args) < 1:
            raise ValueError("'args' kwarg must be a sequence of at least one element")
        if user is not None and os.name == "posix" and not isinstance(user, PosixSessionUser):
            raise ValueError("Argument 'user' must be a PosixSessionUser on posix systems.")
        if user is not None and is_windows() and not isinstance(user, WindowsSessionUser):
            raise ValueError("Argument 'user' must be a WindowsSessionUser on Windows systems.")

        self._logger = logger
        self._args = args[:]  # Make a copy
        self._encoding = encoding
        self._user = user
        self._callback = callback
        self._process = None
        self._os_env_vars = os_env_vars
        self._working_dir = working_dir
        self._start_failed = False
        self._has_started = Event()
        self._pid = None
        self._returncode = None

    @property
    def pid(self) -> Optional[int]:
        if self._pid is not None:
            return self._pid
        return None

    @property
    def exit_code(self) -> Optional[int]:
        """
        :return: None if the process has not yet exited. Otherwise, it returns the exit code of the
            process. This will always be in range of a 32-bit signed integer.
        """
        # The process.wait() in the run() method ensures that the returncode
        # has been set once the subprocess has completed running. Don't poll here...
        # we only want to make the returncode available after the run method has
        # completed its work.
        if self._process is not None:
            return _exit_code_to_32bit_signed(self._process.returncode)
        return _exit_code_to_32bit_signed(self._returncode)

    @property
    def is_running(self) -> bool:
        """
        Determine whether the subprocess is running.
        :return: True if it is running; False otherwise
        """
        # Note: _process is None when either:
        #  a) The process failed to start; or
        #  b) The process has completed, and we've deleted the Popen instance
        return self._has_started.is_set() and self._process is not None

    @property
    def has_started(self) -> bool:
        """Determine whether or not the subprocess has been started yet or not"""
        return self._has_started.is_set()

    @property
    def failed_to_start(self) -> bool:
        """Determine whether the subprocess failed to start."""
        return self._start_failed

    def wait_until_started(self, timeout: Optional[timedelta] = None) -> None:
        """Blocks the caller until the subprocess has been started
        and is either running or has failed to start running.
        Args:
           timeout - Cease waiting after the given number of seconds has elapsed.
        """
        self._has_started.wait(timeout.total_seconds() if timeout is not None else None)

    def run(self) -> None:
        """Run the subprocess. The subprocess cannot be run if it has already been run, or is
        running.
        This is a blocking call.
        """
        if self._has_started.is_set():
            raise RuntimeError("The process has already been run")

        self._process = self._start_subprocess()
        # Set _has_started regardless of whether we started the process successfully or
        # not. That will wake up anyone waiting on wait_until_started() to know whether
        # we've gotten this far.
        self._has_started.set()
        if self._process is None:
            # We failed to start the subprocess
            self._start_failed = True
            if self._callback:
                self._callback()
            return

        self._pid = self._process.pid

        self._logger.info(f"Command started as pid: {self._process.pid}")
        self._logger.info("Output:")

        stream = self._process.stdout
        # Convince type checker that stdout is not None
        assert stream is not None

        # Process stdout/stderr of the job; echoing it to our logger
        def _stream_readline_max_length():
            nonlocal stream
            # Enforce a max line length for readline to ensure we don't infinitely grow the buffer
            return stream.readline(LOG_LINE_MAX_LENGTH)  # type: ignore

        try:
            for line in iter(_stream_readline_max_length, ""):
                line = line.rstrip("\n\r")
                self._logger.info(line)

            self._process.wait()
            self._returncode = self._process.returncode

            if self._callback:
                self._callback()
        finally:
            # Explicitly delete the Popen in case it's a PopenWindowsAsUser and there's stuff to
            # deallocate
            proc = self._process
            self._process = None
            del proc

    def notify(self) -> None:
        """The 'Notify' part of Open Job Description's subprocess cancelation method.
        On Linux/macOS:
            - Send a SIGTERM to the parent process
        On Windows:
            - Send a CTRL_BREAK_EVENT to the process group

        TODO: Send the signal to every direct and transitive child of the parent
        process.
        """
        if self._process is not None and self._process.poll() is None:
            if is_posix():
                self._posix_signal_subprocess(signal="term", signal_subprocesses=False)
            else:
                self._windows_notify_subprocess()

    def terminate(self) -> None:
        """The 'Terminate' part of Open Job Description's subprocess cancelation method.
        On Linux/macOS:
            - Send a SIGKILL to the parent process
        On Windows:
            - Not yet supported.

        TODO: Send the signal to every direct and transitive child of the parent
        process.
        """
        if self._process is not None and self._process.poll() is None:
            if is_posix():
                self._posix_signal_subprocess(signal="kill", signal_subprocesses=True)
            else:
                self._logger.info(
                    f"INTERRUPT: Start killing the process tree with the root pid: {self._process.pid}"
                )
                kill_windows_process_tree(self._logger, self._process.pid, signal_subprocesses=True)

    def _start_subprocess(self) -> Optional[Popen]:
        """Helper invoked by self.run() to start up the subprocess."""
        try:
            command: list[str] = []
            if self._user is not None:
                if is_posix():
                    user = cast(PosixSessionUser, self._user)
                    # Only sudo if the user to run as is not the same as the current user.
                    if not user.is_process_user():
                        # Note: setsid is required; else the running process will be in the
                        # same process group as the `sudo` command. If that happens, then
                        # we're stuck: 1/ Our user cannot kill processes by the self._user; and
                        # 2/ The self._user cannot kill the root-owned sudo process group.
                        command.extend(["sudo", "-u", user.user, "-i", "setsid", "-w"])
                elif is_windows():
                    user = cast(WindowsSessionUser, self._user)  # type: ignore

            command.extend(self._args)

            # Append the given environment to the current one.
            popen_args: dict[str, Any] = dict(
                args=command,
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=STDOUT,
                encoding=self._encoding,
                start_new_session=True,
                cwd=self._working_dir,
            )

            if is_windows():
                # We need a process group in order to send notify signals
                # https://docs.python.org/2/library/subprocess.html#subprocess.CREATE_NEW_PROCESS_GROUP
                popen_args["creationflags"] = CREATE_NEW_PROCESS_GROUP

            cmd_line_for_logger: str
            if is_posix():
                cmd_line_for_logger = shlex.join(command)
            else:
                cmd_line_for_logger = list2cmdline(self._args)
            self._logger.info("Running command %s", cmd_line_for_logger)

            process: Popen
            if is_windows() and self._user and not user.is_process_user():
                popen_args["env"] = self._os_env_vars
                process = PopenWindowsAsUser(user, **popen_args)  # type: ignore
            else:
                if self._os_env_vars:
                    # Our env vars may have 'None' as the value for some keys.
                    # Semantically, that means that we want to delete that environment
                    # variable's value from the environment. However, Popen doesn't do that;
                    # it will instead choose to blow-up if any env var value is None
                    env: dict[str, Optional[str]] = dict(os.environ)
                    env.update(**self._os_env_vars)
                    popen_env: dict[str, str] = {k: v for k, v in env.items() if v is not None}
                    popen_args["env"] = popen_env
                process = Popen(**popen_args)
            return process

        except Exception as e:
            self._logger.info(f"Process failed to start: {str(e)}")
            return None

    def _posix_signal_subprocess(self, signal: str, signal_subprocesses: bool = False) -> None:
        """Send a given named signal, via pkill, to the subprocess when it is running
        as a different user than this process.
        """
        # Convince the type checker that accessing _process is okay
        assert self._process is not None

        # Note: A limitation of this implementation is that it will only sigkill
        # processes that are in the same process-group as the command that we ran.
        # In the future, we can extend this to killing all processes spawned (including into
        # new process-groups since the parent-pid will allow the mapping)
        # by a depth-first traversal through the children. At each recursive
        # step we:
        #  1. SIGSTOP the process, so that it cannot create new subprocesses;
        #  2. Recurse into each child; and
        #  3. SIGKILL the process.
        # Things to watch for when doing so:
        #  a. PIDs can get reused; just because a pid was a child of a process at one point doesn't
        #     mean that it's still the same process when we recurse to it. So, check that the parent-pid
        #     of any child is still as expected before we signal it or collect its children.
        #  b. When we run the command using `sudo` then we need to either run code that does the whole
        #     algorithm as the other user, or `sudo` to send every process signal.

        cmd = list[str]()
        signal_child = False

        if self._user is not None:
            user = cast(PosixSessionUser, self._user)
            # Only sudo if the user to run as is not the same as the current user.
            if not user.is_process_user():
                cmd.extend(["sudo", "-u", user.user, "-i"])
            signal_child = True

        cmd.extend(
            [
                str(POSIX_SIGNAL_SUBPROC_SCRIPT_PATH),
                str(self._process.pid),
                signal,
                str(signal_child),
                str(signal_subprocesses),
            ]
        )
        self._logger.info(f"INTERRUPT: Running: {shlex.join(cmd)}")
        result = run(
            cmd,
            stdout=PIPE,
            stderr=STDOUT,
            stdin=DEVNULL,
        )
        if result.returncode != 0:
            self._logger.warning(
                f"Failed to send signal '{signal}' to subprocess {self._process.pid}: %s",
                result.stdout.decode("utf-8"),
            )

    def _windows_notify_subprocess(self) -> None:
        """Sends a CTRL_BREAK_EVENT signal to the subprocess"""
        # Convince the type checker that accessing _process is okay
        assert self._process is not None

        # CTRL-C handler is disabled by default when CREATE_NEW_PROCESS_GROUP is passed.
        # We send CTRL-BREAK as handler for it cannnot be disabled.
        # https://learn.microsoft.com/en-us/windows/console/ctrl-c-and-ctrl-break-signals
        # https://learn.microsoft.com/en-us/windows/console/generateconsolectrlevent
        # https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessa#remarks
        # https://stackoverflow.com/questions/35772001/how-to-handle-a-signal-sigint-on-a-windows-os-machine/35792192#35792192
        self._logger.info(f"INTERRUPT: Sending CTRL_BREAK_EVENT to {self._process.pid}")

        # _process will be running in new console, we run another process to attach to it and send signal
        cmd = [
            sys.executable,
            str(WINDOWS_SIGNAL_SUBPROC_SCRIPT_PATH),
            str(self._process.pid),
        ]
        result = run(
            cmd,
            stdout=PIPE,
            stderr=STDOUT,
            stdin=DEVNULL,
            creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            self._logger.warning(
                f"Failed to send signal 'CTRL_BREAK_EVENT' to subprocess {self._process.pid}: %s",
                result.stdout.decode("utf-8"),
            )
