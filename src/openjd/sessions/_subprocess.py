# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import shlex
import time
from ._os_checker import is_posix, is_windows

if is_windows():
    from subprocess import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW  # type: ignore
    from ._win32._popen_as_user import PopenWindowsAsUser  # type: ignore
    from ._windows_process_killer import kill_windows_process_tree
from queue import Queue, Empty
from typing import Any
from threading import Event, Thread
from ._logging import LoggerAdapter, LogContent, LogExtraInfo
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
STDOUT_END_GRACETIME_SECONDS = 5


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
            process
        """
        # The process.wait() in the run() method ensures that the returncode
        # has been set once the subprocess has completed running. Don't poll here...
        # we only want to make the returncode available after the run method has
        # completed its work.
        if self._process is not None:
            return self._process.returncode
        return self._returncode

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

        self._logger.info(
            f"Command started as pid: {self._process.pid}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        self._logger.info(
            "Output:",
            extra=LogExtraInfo(openjd_log_content=LogContent.BANNER | LogContent.COMMAND_OUTPUT),
        )

        try:
            self._log_subproc_stdout()  # Blocking
            self._process.wait()
            self._returncode = self._process.returncode
            self._log_returncode()
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
                    f"INTERRUPT: Start killing the process tree with the root pid: {self._process.pid}",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
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
            self._logger.info(
                "Running command %s",
                cmd_line_for_logger,
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.FILE_PATH | LogContent.PROCESS_CONTROL
                ),
            )

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
            self._logger.info(
                f"Process failed to start: {str(e)}",
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.EXCEPTION_INFO | LogContent.PROCESS_CONTROL
                ),
            )
            return None

    def _log_subproc_stdout(self):
        """
        Blocking call which logs the STDOUT of the running subproc until the subprocess exits.

        Note that this can result in hanging threads if:
            1. The command we run creates a detatched grandchild process
            2. The detached grandchild inherits the STDOUT stream of the process we run
            3. The detached grandchild process does not write to STDOUT, ever.

        In the above situation, there is a thread leak due to there not being a unilaterally
        available python API which performs a timed-out blocking read on IO streams. If the
        grandchild process ever:
            1. Exits
            2. Writes to STDOUT
        or
            3. The python session running this code ends

        Then the thread exits and is cleaned up automatically by the python garbage collector.
        """
        assert self._process
        stream = self._process.stdout
        # Convince type checker that stdout is not None
        assert stream is not None

        exit_event = Event()

        # Process stdout/stderr of the job; echoing it to our logger
        def _stream_readline_max_length():
            if exit_event.is_set():
                return ""  # we can return anything here, just forces the iter to loop
            # Enforce a max line length for readline to ensure we don't infinitely grow the buffer
            return stream.readline(LOG_LINE_MAX_LENGTH)  # type: ignore

        stdout_queue: Queue[str] = Queue()

        def _enqueue_stdout():
            """
            Enqueues all the stdout from stream into the given queue, until the stream is closed
            or exit_event is set.
            """
            for line in iter(_stream_readline_max_length, ""):
                if exit_event.is_set():
                    break
                line = line.rstrip("\n\r")
                stdout_queue.put(line)
            stream.close()

        process_exit_time = None
        warn_time = None

        logging_thread = Thread(target=_enqueue_stdout, daemon=True)
        # We start the thread as a daemon, and explicitly do not call join() on it ever because:
        #      If the subprocess creates a child subprocess that inherits the STDOUT stream, then exits while leaving
        #      the child process running. There is an edge case where the child process never write to STDOUT and we are
        #      stuck waiting on stream.readline() to stop blocking before exit_event can be checked to end the thread.
        #      In this case we leave a dangling thread until the subprocess exits. This was done because
        #       1. Python does not have support for a blocking read with a timeout.
        #       2. Python does not have unilateral support for non-blocking reads until Python 3.12 and non-blocking
        #          reads would require arbitrary sleep calls while waiting for output which could cause a performance
        #          impact.
        logging_thread.start()

        while (
            logging_thread.is_alive()
        ):  # If the logging thread is alive, the STDOUT stream is still open
            try:
                # We timeout after 1 ms because the main process can end while leaving child processes that
                # prevent closing the STDOUT stream. Waiting a maximum of 1 ms allows us to detect this quickly, while
                # not significantly impacting CPU usage.
                line = stdout_queue.get(timeout=0.001)
                self._logger.info(
                    line, extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT)
                )
            except Empty:
                pass  # queue.get timed out. This means the subprocess does not print much to STDOUT. Just continue.

            if self._process.poll() is not None:  # The main command exited.
                if process_exit_time is None:
                    process_exit_time = time.monotonic()
                elif (time.monotonic() - process_exit_time) < 1:
                    # There could be a bunch of STDOUT buffered up. We don't want the warning to be too noisy, so
                    # give a second to clear the queue before we get stern and warn about ending the action.
                    continue
                elif not warn_time:
                    # It's been over a second of trying to empty STDOUT. Most likely the stream is still open.
                    self._logger.warning(
                        f"Command exited but STDOUT stream is still open. Waiting gracetime of {STDOUT_END_GRACETIME_SECONDS} seconds for the STDOUT stream to close before ending action.",
                        extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                    )
                    warn_time = time.monotonic()
                elif (time.monotonic() - warn_time) > STDOUT_END_GRACETIME_SECONDS:
                    self._logger.warning(
                        f"Gracetime of {STDOUT_END_GRACETIME_SECONDS} seconds elapsed but the STDOUT stream is still open. Ending action.",
                        extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                    )
                    exit_event.set()  # When the STDOUT stream ends this will cause the thread to exit.
                    break

        while not stdout_queue.empty():
            # empty the queue
            line = stdout_queue.get()
            self._logger.info(
                line, extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT)
            )

    def _log_returncode(self):
        """Logs the return code of the exited subprocess"""
        if self._returncode is not None:
            # Print out the signed representation of returncodes that would be negative as a 32-bit signed integer
            if self._returncode < 0x7FFFFFFF:
                self._logger.info(
                    f"Process pid {self._process.pid} exited with code: {self._returncode} (unsigned) / {hex(self._returncode)} (hex)",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )
            else:

                def _tosigned(n: int) -> int:
                    b = (n & 0xFFFFFFFF).to_bytes(4, "big", signed=False)
                    return int.from_bytes(b, "big", signed=True)

                self._logger.info(
                    f"Process pid {self._process.pid} exited with code: {self._returncode} (unsigned) / {hex(self._returncode)} (hex) / {_tosigned(self._returncode)} (signed)",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )

    def _posix_signal_subprocess(self, signal: str, signal_subprocesses: bool = False) -> None:
        """Send a given named signal, via pkill, to the subprocess when it is running
        as a different user than this process.
        """
        # We can run into a race condition where the process exits (and another thread sets self._process to None)
        # before the cancellation happens, so we swap to a local variable to ensure a cancellation that is not needed,
        # does not raise an exception here.
        process = self._process
        # Convince the type checker that accessing process is okay
        assert process is not None

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
                str(process.pid),
                signal,
                str(signal_child),
                str(signal_subprocesses),
            ]
        )
        self._logger.info(
            f"INTERRUPT: Running: {shlex.join(cmd)}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        result = run(
            cmd,
            stdout=PIPE,
            stderr=STDOUT,
            stdin=DEVNULL,
        )
        if result.returncode != 0:
            self._logger.warning(
                f"Failed to send signal '{signal}' to subprocess {process.pid}: %s",
                result.stdout.decode("utf-8"),
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                ),
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
        self._logger.info(
            f"INTERRUPT: Sending CTRL_BREAK_EVENT to {self._process.pid}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )

        # _process will be running in new console, we run another process to attach to it and send signal
        cmd = [
            # When running in a service context, we want to call the non-service Python binary
            sys.executable.lower().replace("pythonservice.exe", "python.exe"),
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
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                ),
            )
