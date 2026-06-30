# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import glob
import os
import sys
import time
from subprocess import Popen, DEVNULL, PIPE, STDOUT, run
from typing import Optional

from .._logging import LoggerAdapter, LogContent, LogExtraInfo
from .._os_checker import is_posix, is_linux


class FindSignalTargetError(Exception):
    """Exception when unable to detect the signal target"""

    pass


def find_sudo_child_process_group_id(
    *,
    logger: LoggerAdapter,
    sudo_process: Popen,
    timeout_seconds: float = 1,
) -> Optional[int]:
    # Hint to mypy to not raise module attribute errors (e.g. missing os.getpgid)
    if sys.platform == "win32":
        raise NotImplementedError("This method is for POSIX hosts only")
    if not is_posix():
        raise NotImplementedError(f"Only POSIX supported, but running on {sys.platform}")
    if timeout_seconds <= 0:
        raise ValueError(f"Expected positive value for timeout_seconds but got {timeout_seconds}")

    # For cross-user support, we use sudo which creates an intermediate process:
    #
    #    openjd-process
    #      |
    #      +-- sudo
    #            |
    #            +-- subprocess
    #
    # Sudo forwards signals that it is able to handle, but in the case of SIGKILL sudo cannot
    # handle the signal and the kernel will kill it leaving the child orphaned. We need to
    # send SIGKILL signals to the subprocess of sudo
    start = time.monotonic()
    now = start
    sudo_pgid = os.getpgid(sudo_process.pid)

    # Repeatedly scan for child processes
    #
    # This is put in a retry loop, because it takes a non-zero amount of time before sudo and
    # the kernel finish creating the subprocess. We cap this because the process may exit
    # quickly and we may never find the child process.
    sudo_child_pid: Optional[int] = None
    sudo_child_pgid: Optional[int] = None
    try:
        while now - start < timeout_seconds:
            if not sudo_child_pid:
                if is_linux():
                    sudo_child_pid = find_sudo_child_process_id_procfs(
                        sudo_pid=sudo_process.pid,
                        logger=logger,
                    )
                else:
                    sudo_child_pid = find_child_process_id_pgrep(
                        sudo_pid=sudo_process.pid,
                    )

            if sudo_child_pid:
                try:
                    sudo_child_pgid = os.getpgid(sudo_child_pid)
                except ProcessLookupError:
                    # If the process has exited, we short-circuit
                    return None
                # sudo first forks, then creates a new process group. There is a race condition
                # where the process group ID we observe has not yet changed. If the PGID detected
                # matches the PGID of sudo, then we retry again in the loop
                if sudo_child_pgid == sudo_pgid:
                    sudo_child_pgid = None
                else:
                    break

            # If we did not find any child processes yet, sleep for some time and retry
            time.sleep(min(0.05, timeout_seconds - (now - start)))
            now = time.monotonic()
        if not sudo_child_pid or not sudo_child_pgid:
            raise FindSignalTargetError("unable to detect subprocess before timeout")
    except FindSignalTargetError as e:
        logger.warning(
            f"Unable to determine signal target: {e}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )

    if sudo_child_pgid:
        logger.debug(
            f"Signal target PGID = {sudo_child_pgid}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )

    return sudo_child_pgid


def find_sudo_child_process_id_procfs(
    *,
    logger: LoggerAdapter,
    sudo_pid: int,
) -> Optional[int]:
    # Look for the child process of sudo using procfs. See
    # https://docs.kernel.org/filesystems/proc.html#proc-pid-task-tid-children-information-about-task-children

    child_pids: set[int] = set()
    for task_children_path in glob.glob(f"/proc/{sudo_pid}/task/**/children"):
        with open(task_children_path, "r") as f:
            child_pids.update(int(pid_str) for pid_str in f.read().split())

    # If we found exactly one child, we return it
    if len(child_pids) == 1:

        child_pid = child_pids.pop()

        logger.debug(
            f"Session action process (sudo child) PID is {child_pid}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        return child_pid
    # If we found multiple child processes, this violates our assumptions about how sudo
    # works. We will fall-back to using pkill for signalling the process
    elif len(child_pids) > 1:
        raise FindSignalTargetError(
            f"Expected single child processes of sudo, but found {child_pids}"
        )
    return None


def find_child_process_id_pgrep(
    *,
    sudo_pid: int,
) -> Optional[int]:
    pgrep_result = run(
        ["pgrep", "-P", str(sudo_pid)],
        stdout=PIPE,
        stderr=STDOUT,
        stdin=DEVNULL,
        text=True,
    )
    if pgrep_result.returncode != 0:
        raise FindSignalTargetError("Unable to query child processes of sudo process")
    results = pgrep_result.stdout.splitlines()
    if len(results) > 1:
        raise FindSignalTargetError(f"Expected a single child process of sudo, but found {results}")
    elif len(results) == 0:
        return None
    sudo_subproc_pid = int(results[0])
    return sudo_subproc_pid
