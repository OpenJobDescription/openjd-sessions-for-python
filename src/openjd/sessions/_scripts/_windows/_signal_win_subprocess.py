# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
from psutil import Process
from signal import CTRL_BREAK_EVENT  # type: ignore
from win32console import AttachConsole, FreeConsole  # type: ignore


def send_ctrl_break_event(pgid: int):
    """Sends a CTRL_BREAK_EVENT to a process group id to signal process to shut down"""
    process = Process(pgid)

    # Send signal can only target processes in the same console.
    # We first detach from the current console and re-attach to that of process group.
    FreeConsole()
    AttachConsole(pgid)

    # Send the signal
    process.send_signal(CTRL_BREAK_EVENT)


pgid = int(sys.argv[1])
send_ctrl_break_event(pgid)
