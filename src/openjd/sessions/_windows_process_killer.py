# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import time

from ._logging import LogExtraInfo, LogContent
from psutil import NoSuchProcess, Process, wait_procs, STATUS_STOPPED
from typing import List


def _suspend_process(logger, process: Process) -> bool:
    """
    Suspend a given process.

    Parameters:
    - logger: The logging instance for logging.
    - process: The process to be suspended.

    Returns:
    - True if the process was successfully suspended, False otherwise.
    """
    try:
        process.suspend()
        for _ in range(10):
            if process.status() == STATUS_STOPPED:
                return True
            # Wait for the process to be suspended
            time.sleep(0.1)
    except Exception as e:
        logger.error(
            f"Failed to suspend process {process.pid}: {e}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        return False

    return False


def _suspend_process_tree(
    logger,
    process: Process,
    all_processes: List[Process],
    procs_cannot_suspend: List[Process],
    suspend_subprocesses: bool,
) -> None:
    """
    Recursively suspend the process tree and its children in pre-order.

    Parameters:
    - logger: The logging instance for logging.
    - process: The process to start suspension from.
    - all_processes: List of all processes we are trying to suspend.
    - procs_cannot_suspend: List of processes that couldn't be suspended.
    - suspend_subprocesses: Control if the child processes needed to be suspended
    """
    # Attempt to suspend the current process.
    if not _suspend_process(logger, process):
        procs_cannot_suspend.append(process)

    all_processes.append(process)

    if not suspend_subprocesses:
        return

    # Recursively suspend child processes.
    for child in process.children():
        _suspend_process_tree(
            logger, child, all_processes, procs_cannot_suspend, suspend_subprocesses
        )


def _kill_processes(logger, process_list: List[Process]) -> List[Process]:
    """
    Kill all processes in the given list.

    Parameters:
    - logger: The logging instance for logging.
    - process_list: List of processes to be killed.

    Returns:
    - List of processes that are still alive after attempting to kill.
    """

    for process in process_list:
        try:
            logger.info(
                f"Killing process with id {process.pid}.",
                extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
            )
            process.kill()
        except NoSuchProcess:
            logger.info(
                f"No process with id {process.pid} for termination",
                extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
            )
        except Exception as e:
            logger.error(
                f"Failed to kill process {process.pid}: {e}",
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                ),
            )

    # Wait for the processes to be terminated
    _, alive = wait_procs(process_list, timeout=5)
    return alive


def kill_windows_process_tree(logger, root_pid, signal_subprocesses=True) -> None:
    """
    Kills the windows process tree starting from the provided root PID.
    The termination ordering will be from leafs to root.

    Parameters:
    - logger: The logging instance to record activities.
    - root_pid: The root process ID.
    - signal_subprocesses: Flag to determine if subprocesses should be signaled.
    """

    try:
        parent_process = Process(root_pid)
    except NoSuchProcess:
        logger.error(
            f"Root process with PID {root_pid} not found.",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        return

    procs_failed_to_suspend: List[Process] = []
    processes_to_be_killed: List[Process] = []
    _suspend_process_tree(
        logger, parent_process, processes_to_be_killed, procs_failed_to_suspend, signal_subprocesses
    )
    if procs_failed_to_suspend:
        logger.warning(
            f"Following processes cannot be suspended. Processes IDs: {[proc.pid for proc in procs_failed_to_suspend]}",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )

    # Ensure we kill child processes first
    processes_to_be_killed.reverse()
    alive_processes = _kill_processes(logger, processes_to_be_killed)

    if alive_processes:
        logger.warning(
            f"Failed to kill following process(es): {[p.pid for p in alive_processes]}. Retrying...",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        alive_processes = _kill_processes(logger, processes_to_be_killed)
        if alive_processes:
            logger.warning(
                f"Still failed to kill the following process(es): {[p.pid for p in alive_processes]}. Please handle manually.",
                extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
            )
