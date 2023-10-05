# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import time
from psutil import NoSuchProcess, Process, wait_procs, STATUS_STOPPED
from typing import List, Tuple


def _wait_for_all_suspended(
    logger, root_process: Process, signal_subprocesses: bool
) -> Tuple[List[Process], List[Process]]:
    """
    Wait until all processes in the tree have been stopped or the timeout is reached.

    Parameters:
    - logger: The logging instance to record logging.
    - root_process: The root process from which we'll track all subprocesses.
    - signal_subprocesses: Flag to determine if all subprocesses should be suspended.

    Returns:
    - Tuple containing:
        - List of processes that couldn't be suspended.
        - List of all processes that were trying for suspension.
    """

    procs_cannot_suspend: List[Process] = []
    all_processes: List[Process] = []
    # We give a timeout of 5 seconds for suspension
    end_time = time.time() + 5

    while time.time() < end_time:
        procs_cannot_suspend = []
        all_processes = [root_process]
        if signal_subprocesses:
            all_processes += root_process.children(recursive=True)

        for process in all_processes:
            if process.status() == STATUS_STOPPED:
                continue
            try:
                process.suspend()
            except Exception as e:
                procs_cannot_suspend.append(process)
                logger.error(f"Failed to suspend process {process.pid}: {e}")

        all_suspended = all([proc.status() == STATUS_STOPPED for proc in all_processes])
        if all_suspended:
            logger.info(f"Suspended all processes with ids {[proc.pid for proc in all_processes]}")
            break

        time.sleep(0.2)

    return procs_cannot_suspend, all_processes


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
            logger.info(f"Killing process with id {process.pid}.")
            process.kill()
        except NoSuchProcess:
            logger.info(f"No process with id {process.pid} for termination")
        except Exception as e:
            logger.error(f"Failed to kill process {process.pid}: {e}")

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
        logger.error(f"Root process with PID {root_pid} not found.")
        return

    procs_failed_to_suspend, processes_to_be_killed = _wait_for_all_suspended(
        logger, parent_process, signal_subprocesses
    )
    if procs_failed_to_suspend:
        logger.warning(
            f"Following processes cannot be suspended. Processes IDs: {[proc.pid for proc in procs_failed_to_suspend]}"
        )

    # Ensure we kill child processes first
    processes_to_be_killed.reverse()
    alive_processes = _kill_processes(logger, processes_to_be_killed)

    if alive_processes:
        logger.warning(
            f"Failed to kill following process(es): {[p.pid for p in alive_processes]}. Retrying..."
        )
        alive_processes = _kill_processes(logger, processes_to_be_killed)
        if alive_processes:
            logger.warning(
                f"Still failed to kill the following process(es): {[p.pid for p in alive_processes]}. Please handle manually."
            )
