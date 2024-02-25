# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys
from enum import Enum
from typing import Any

from ._os_checker import is_windows

if is_windows():
    import ctypes
    import win32process
    from ctypes import wintypes
    from subprocess import Handle, list2cmdline, Popen  # type: ignore
    from ._session_user import WindowsSessionUser
    from ._win32api_helpers import (
        environment_block_for_user,
        environment_dict_from_block,
        environment_dict_to_block,
    )

# Tell type checker to ignore on non-windows platforms
assert sys.platform == "win32"

advapi32 = ctypes.WinDLL("advapi32")
kernel32 = ctypes.WinDLL("kernel32")

# Constants
CREATE_UNICODE_ENVIRONMENT = 0x400
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
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-process_information
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class BaseEnvironment(Enum):
    TARGET_USER = 0
    """Supplied environment variables supercede target user default environment"""
    NONE = 2
    """Supplied environment variables are the only environment variables."""
    INHERIT = 1
    """Supplied environment variables supercede inherited environment variables of current process"""


class PopenWindowsAsUser(Popen):
    """Class to run a process as another user on Windows.
    Derived from Popen, it defines the _execute_child() method to call CreateProcessWithLogonW.
    """

    _base_environment: BaseEnvironment = BaseEnvironment.TARGET_USER
    _user: WindowsSessionUser

    def __init__(
        self,
        *args: Any,
        base_environment: BaseEnvironment = BaseEnvironment.TARGET_USER,
        user: WindowsSessionUser,
        **kwargs: Any,
    ):
        """
        Arguments:
            username (str):  Name of user to run subprocess as
            password (str):  Password for username
            args (Any):  Popen constructor args
            kwargs (Any):  Popen constructor kwargs
            https://docs.python.org/3/library/subprocess.html#popen-constructor
        """
        self._base_environment = base_environment
        self._user = user
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
        si.cb = ctypes.sizeof(STARTUPINFO)
        pi = PROCESS_INFORMATION()

        si.hStdInput = int(p2cread)
        si.hStdOutput = int(c2pwrite)
        si.hStdError = int(errwrite)
        si.dwFlags |= win32process.STARTF_USESTDHANDLES

        base_env = {}
        if self._base_environment == BaseEnvironment.TARGET_USER:
            env_ptr = environment_block_for_user(self._user.logon_token)
            base_env = environment_dict_from_block(env_ptr)
        elif self._base_environment == BaseEnvironment.INHERIT:
            base_env = dict(os.environ)
        elif self._base_environment == BaseEnvironment.NONE:
            base_env = {}
        else:
            raise NotImplementedError(
                f"base_environment of {self._base_environment.value} not implemented"
            )

        merged_env = base_env.copy()
        if env:
            merged_env.update(env)
        # Sort env vars by keys
        merged_env = {key: merged_env[key] for key in sorted(merged_env.keys())}
        env_ptr = environment_dict_to_block(merged_env)

        try:
            if not self._user.logon_token:
                # https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw
                if not advapi32.CreateProcessWithLogonW(
                    self._user.user,
                    None, # domain
                    self._user.password,
                    LOGON_WITH_PROFILE,
                    executable,
                    commandline,
                    creationflags,
                    env,
                    cwd,
                    ctypes.byref(si),
                    ctypes.byref(pi),
                ):
                    raise ctypes.WinError()
            elif not advapi32.CreateProcessAsUserW(
                self._user.logon_token,
                executable,
                commandline,
                None,
                None,
                True,
                creationflags | CREATE_UNICODE_ENVIRONMENT,
                env_ptr,
                cwd,
                ctypes.byref(si),
                ctypes.byref(pi),
            ):
                raise ctypes.WinError()
        finally:
            # Child is launched. Close the parent's copy of those pipe
            # handles that only the child should have open.
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

        # Retain the process handle, but close the thread handle
        kernel32.CloseHandle(pi.hThread)

        self._child_created = True
        self.pid = pi.dwProcessId
        self._handle = Handle(pi.hProcess)
