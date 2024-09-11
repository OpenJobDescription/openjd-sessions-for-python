# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
import shutil
from logging import INFO, getLogger
from logging.handlers import QueueHandler
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Lock
from typing import Optional, Sequence

from .._session_user import SessionUser
from .._subprocess import LoggingSubprocess
from .._logging import LoggerAdapter

_internal_logger_lock = Lock()
_internal_logger = getLogger("openjd_sessions_runner_base_internal_logger")
_internal_logger_adapter = LoggerAdapter(_internal_logger, extra=dict())
_internal_logger.setLevel(INFO)
_internal_logger.propagate = False
_internal_logger_queue: SimpleQueue = SimpleQueue()
_internal_logger.addHandler(QueueHandler(_internal_logger_queue))


def locate_windows_executable(
    args: Sequence[str],
    user: Optional[SessionUser],
    os_env_vars: Optional[dict[str, Optional[str]]],
    working_dir: str,
) -> Sequence[str]:
    cmd_path = Path(args[0])
    if cmd_path.is_absolute():
        # If it's an absolute path (e.g. C:\Foo\Bar.exe or C:\Foo\Bar) then we just return
        # and leave it up to the OS to resolve the executable's extention.

        # TODO: Do we actually still want to do the find as a check to see if the command exists &
        # is executable? This would catch stuff like 'c:\Foo\test.ps1' as a command (which fails)
        return args

    return_args = list(args)
    if user is None:
        return_args[0] = _locate_for_same_user(cmd_path, os_env_vars, working_dir)
    else:
        return_args[0] = _locate_for_other_user(cmd_path, os_env_vars, working_dir, user)
    return return_args


def _get_path_var_for_shutil_which(
    os_env_vars: Optional[dict[str, Optional[str]]], working_dir: str
):
    path_var: Optional[str] = None
    if os_env_vars:
        env_var_keys = {k.lower(): k for k in os_env_vars}
        path_var = os_env_vars.get(env_var_keys["path"]) if "path" in env_var_keys else None
    if path_var is None:
        path_var = os.environ.get("PATH", "")
    path_var = "%s;%s" % (working_dir, path_var)
    return path_var


def _locate_for_same_user(
    command: Path, os_env_vars: Optional[dict[str, Optional[str]]], working_dir: str
) -> str:
    path_var: Optional[str] = _get_path_var_for_shutil_which(os_env_vars, working_dir)
    exe = str(shutil.which(str(command), path=path_var))
    if not exe:
        raise RuntimeError("Could not find executable file: %s" % command)
    return exe


def _locate_for_other_user(
    command: Path,
    os_env_vars: Optional[dict[str, Optional[str]]],
    working_dir: str,
    user: SessionUser,
) -> str:  # pragma: nocover
    # Running as a potentially different user, so it's possible that
    # this process doesn't have read access to the executable file's location.
    # Thus, we need to rely on running a subprocess as the user to be able
    # to find the executable.

    if len(command.parts) > 1:
        # Windows cannot find executables by relative location
        # i.e. where "dir\test.bat"
        #
        # Even if that worked, we'd have to prepend the relative part of the command
        # to the path and then search for only the command.name. But, we don't generally
        # have the user's PATH env var value.
        #
        # So, for both of those reasons we just return the command and let the action fail out
        # naturally.
        return str(command)

    # Prevent issues that might arise by having multiple Actions trying to start up
    # concurrently -- grab a lock.
    with _internal_logger_lock:
        # Drain the message queue to ensure nothing remains from previous runs.
        try:
            while True:
                _internal_logger_queue.get(block=False)
        except Empty:
            pass  # Will happen when the queue is fully empty

        # When running in a service context, we want to call the non-service Python binary
        sys_executable = sys.executable.lower().replace("pythonservice.exe", "python.exe")

        # In the subprocess code, we avoid exit code 1 as that is returned if a Python exception is thrown.
        exit_code_success = 2
        exit_code_could_not_find_exe = 3

        path_var: Optional[str] = _get_path_var_for_shutil_which(os_env_vars, working_dir)
        process = LoggingSubprocess(
            logger=_internal_logger_adapter,
            args=[
                sys_executable,
                "-c",
                # Command injection here is possible, but it's irrelevant. The command is running
                # as the given user. No need for an attacker to be fancy here, they could just run
                # the desired attack command directly in the job template.
                "import shutil, sys, pathlib\n"
                + f"cmd = shutil.which({str(command)!r}, path={path_var!r})\n"
                + "if cmd:\n"
                + "  print(str(pathlib.Path(cmd).absolute()))\n"
                + f"  sys.exit({exit_code_success})\n"
                + f"sys.exit({exit_code_could_not_find_exe})\n",
            ],
            user=user,
            os_env_vars=os_env_vars,
            working_dir=str(working_dir),
        )
        process.run()  # blocking call
        exit_code = process.exit_code

        # We're seeing random errors when trying to run an Action's command immediately after this
        # outside of Session 0; theory is that maybe this has something to do with running two
        # CreateProcessWithLogonW calls back-to-back with little time inbetween. So, explicitly
        # delete the process object to try to force some cleanup of handles that maybe help the
        # profile get unloaded. (this seems like it might be doing the trick)
        # Error:
        #  [WinError 1018] Illegal operation attempted on a registry key that has been marked for deletion
        del process

        if exit_code == exit_code_could_not_find_exe:
            raise RuntimeError(f"Could not find executable file: {command}")

        # Parse the output
        try:
            while True:
                record = _internal_logger_queue.get(block=False)
                message = record.getMessage()
                if "Output:" in message:
                    break

            if exit_code == exit_code_success:
                exe_record = _internal_logger_queue.get(block=False)
                # The line of output with the result of 'shutil.which' is the location of the command
                return exe_record.getMessage()
        except Empty:
            raise RuntimeError(
                f"Could not run Python as user {user.user} to find executable {command} in PATH.\n"
                + f"The host configuration must allow users to run {sys_executable}."
            )

        # Collect the error output from the subprocess
        error_messages = []
        try:
            while True:
                record = _internal_logger_queue.get(block=False)
                error_messages.append(record.getMessage())
        except Empty:
            pass

        # Something went wrong in launching sys_executable.
        # Because this scenario may be difficult to diagnose, we include more context.
        raise RuntimeError(
            f"Could not run Python as user {user.user} to find executable {command} in PATH.\n"
            + f"The host configuration must allow users to run {sys_executable}.\n\nError output:\n"
            + "\n".join(error_messages)
        )
