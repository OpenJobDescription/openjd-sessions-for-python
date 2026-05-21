# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Note: This script will only be run as a subprocess.

import sys
import ctypes
from ctypes.wintypes import BOOL, DWORD

# This assertion short-circuits mypy from type checking this module on platforms other than Windows
# https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
assert sys.platform == "win32"

kernel32 = ctypes.WinDLL("kernel32")

kernel32.AllocConsole.restype = BOOL
kernel32.AllocConsole.argtypes = []

# https://learn.microsoft.com/en-us/windows/console/attachconsole
kernel32.AttachConsole.restype = BOOL
kernel32.AttachConsole.argtypes = [
    DWORD,  # [in] dwProcessId
]

# https://learn.microsoft.com/en-us/windows/console/freeconsole
kernel32.FreeConsole.restype = BOOL
kernel32.FreeConsole.argtypes = []

kernel32.GenerateConsoleCtrlEvent.restype = BOOL
kernel32.GenerateConsoleCtrlEvent.argtypes = [
    DWORD,  # [in] dwCtrlEvent
    DWORD,  # [in] dwProcessGroupId
]

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1

ATTACH_PARENT_PROCESS = -1


def signal_process(pgid: int):
    # Send signal can only target processes in the same console.
    # We first detach from the current console and re-attach to that of process group.
    if not kernel32.FreeConsole():
        raise ctypes.WinError()
    if not kernel32.AttachConsole(pgid):
        raise ctypes.WinError()

    # Send the signal
    # We send CTRL-BREAK as handler for it cannnot be disabled.
    # https://learn.microsoft.com/en-us/windows/console/ctrl-c-and-ctrl-break-signals

    # We only send CTRL-BREAK
    # if not kernel32.GenerateConsoleCtrlEvent(CTRL_C_EVENT, pgid):
    #     raise ctypes.WinError()
    if not kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pgid):
        raise ctypes.WinError()


if __name__ == "__main__":
    signal_process(int(sys.argv[1]))
