# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
from ._os_checker import is_windows

if is_windows():
    import ctypes
    import win32process
    from ctypes import wintypes
    from subprocess import Handle, list2cmdline, Popen  # type: ignore

assert sys.platform == "win32"

advapi32 = ctypes.WinDLL("advapi32")
kernel32 = ctypes.WinDLL("kernel32")

# Constants
LOGON_WITH_PROFILE = 0x00000001


# Structures
class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class PopenWindowsAsUser(Popen):
    def __init__(self, username, password, *args, **kwargs):
        self.username = username
        self.password = password
        self.domain = None
        super(PopenWindowsAsUser, self).__init__(*args, **kwargs)

    def _execute_child(
        self,
        args,
        executable,
        preexec_fn,
        close_fds,
        pass_fds,
        cwd,
        env,
        startupinfo,
        creationflags,
        shell,
        p2cread,
        p2cwrite,
        c2pread,
        c2pwrite,
        errread,
        errwrite,
        restore_signals,
        start_new_session,
        *additional_args,
        **kwargs,
    ):
        """Execute program (MS Windows version)"""

        assert not pass_fds, "pass_fds not supported on Windows."

        commandline = args if isinstance(args, str) else list2cmdline(args)

        # Initialize structures
        si = STARTUPINFO()
        si.cb = ctypes.sizeof(STARTUPINFO)
        pi = PROCESS_INFORMATION()

        si.hStdInput = int(p2cread)
        si.hStdOutput = int(c2pwrite)
        si.hStdError = int(errwrite)
        si.dwFlags |= win32process.STARTF_USESTDHANDLES

        result = advapi32.CreateProcessWithLogonW(
            self.username,
            self.domain,
            self.password,
            LOGON_WITH_PROFILE,
            executable,
            commandline,
            creationflags,
            env,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )

        if not result:
            raise ctypes.WinError()

        # Child is launched. Close the parent's copy of those pipe
        # handles that only the child should have open.
        self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

        # Retain the process handle, but close the thread handle
        kernel32.CloseHandle(pi.hThread)

        self._child_created = True
        self.pid = pi.dwProcessId
        self._handle = Handle(pi.hProcess)
