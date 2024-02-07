# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
from ._os_checker import is_windows
from typing import Any

if is_windows():
    import ctypes
    import win32process
    from ctypes import wintypes
    from subprocess import Handle, list2cmdline, Popen  # type: ignore

# Tell type checker to ignore on non-windows platforms
assert sys.platform == "win32"

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Constants
LOGON_WITH_PROFILE = 0x00000001


# Structures
# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfoa
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
        # ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("lpReserved2", wintypes.LPBYTE),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

    def __init__(self, *args, **kwds):
        self.cb = ctypes.sizeof(self)
        super(STARTUPINFO, self).__init__(*args, **kwds)


class MYHANDLE(wintypes.HANDLE):
    def detach(self):
        handle, self.value = self.value, None
        return wintypes.HANDLE(handle)

    def close(self, CloseHandle=kernel32.CloseHandle):
        if self:
            CloseHandle(self.detach())

    def __del__(self):
        self.close()


# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-process_information
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        # ("hProcess", MYHANDLE),
        # ("hThread", MYHANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


LPPROCESS_INFORMATION = ctypes.POINTER(PROCESS_INFORMATION)
LPSTARTUPINFO = ctypes.POINTER(STARTUPINFO)


class PopenWindowsAsUser(Popen):
    """Class to run a process as another user on Windows.
    Derived from Popen, it defines the _execute_child() method to call CreateProcessWithLogonW.
    """

    def __init__(self, username: str, password: str, *args: Any, **kwargs: Any):
        """
        Arguments:
            username (str):  Name of user to run subprocess as
            password (str):  Password for username
            args (Any):  Popen constructor args
            kwargs (Any):  Popen constructor kwargs
            https://docs.python.org/3/library/subprocess.html#popen-constructor
        """
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
        """Execute program (MS Windows version).
        Calls CreateProcessWithLogonW to run a process as another user.
        """

        assert not pass_fds, "pass_fds not supported on Windows."

        commandline = args if isinstance(args, str) else list2cmdline(args)

        # Initialize structures
        si = STARTUPINFO()
        # si.cb = ctypes.sizeof(STARTUPINFO)
        pi = PROCESS_INFORMATION()

        si.hStdInput = int(p2cread)
        si.hStdOutput = int(c2pwrite)
        si.hStdError = int(errwrite)
        si.dwFlags |= win32process.STARTF_USESTDHANDLES

        def _check_bool(result, func, args):
            if not result:
                # raise ctypes.WinError(ctypes.get_last_error())
                print("matta err check")
                raise ctypes.WinError()
            return args

        advapi32.CreateProcessWithLogonW.errcheck = _check_bool
        advapi32.CreateProcessWithLogonW.argtypes = (
            wintypes.LPCWSTR,  # lpUsername
            wintypes.LPCWSTR,  # lpDomain
            wintypes.LPCWSTR,  # lpPassword
            wintypes.DWORD,  # dwLogonFlags
            wintypes.LPCWSTR,  # lpApplicationName
            wintypes.LPWSTR,  # lpCommandLine (inout)
            wintypes.DWORD,  # dwCreationFlags
            wintypes.LPCWSTR,  # lpEnvironment  (force Unicode)
            wintypes.LPCWSTR,  # lpCurrentDirectory
            LPSTARTUPINFO,  # lpStartupInfo
            LPPROCESS_INFORMATION,  # lpProcessInfo (out)
        )

        try:
            result = None
            # https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw
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

        finally:
            # Child is launched. Close the parent's copy of those pipe
            # handles that only the child should have open.
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

            if not result:
                print("not result")
            # raise ctypes.WinError()

            if not pi.hProcess:
                raise ctypes.WinError()

        # Retain the process handle, but close the thread handle
        kernel32.CloseHandle(pi.hThread)

        self._child_created = True
        self.pid = pi.dwProcessId
        print("In Proc. pid", self.pid)
        print(type(pi.hProcess))

        if not pi.hProcess:
            raise ctypes.WinError()

        self._handle = Handle(pi.hProcess)
        # kself._handle = Handle(int.from_bytes(pi.hProcess))
