# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import sys

# This assertion short-circuits mypy from type checking this module on platforms other than Windows
# https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
assert sys.platform == "win32"

from typing import Any, Optional, cast
import ctypes
from ctypes.wintypes import HANDLE
from subprocess import list2cmdline, Popen
from subprocess import Handle  # type: ignore # linter doesn't know it exists
import platform
from ._api import (
    # Constants
    LOGON_WITH_PROFILE,
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    STARTF_USESHOWWINDOW,
    STARTF_USESTDHANDLES,
    SW_HIDE,
    # Structures
    PROCESS_INFORMATION,
    STARTUPINFO,
    STARTUPINFOEX,
    # Functions
    CloseHandle,
    CreateProcessAsUserW,
    CreateProcessWithLogonW,
    DestroyEnvironmentBlock,
    UpdateProcThreadAttribute,
)
from ._helpers import (
    environment_block_for_user,
    # environment_block_for_user_context,
    environment_block_from_dict,
    environment_block_to_dict,
    logon_user_context,
)
from .._session_user import WindowsSessionUser

if platform.python_implementation() != "CPython":
    raise RuntimeError(
        f"Not compatible with the {platform.python_implementation} of Python. Please use CPython."
    )

CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000


def inherit_handles(startup_info: STARTUPINFOEX, handles: tuple[int]) -> ctypes.Array:
    """Set the given 'startup_info' to have the subprocess inherit the given handles, and only the
    given handles.

    Returns:
        - The allocated list of handles. Per the Win32 APIs, you must ensure that this buffer is
          only deallocated *after* the attribute list in the startup_info has been deallocated.
    """
    handles_list = (HANDLE * len(handles))()
    for i, h in enumerate(handles):
        handles_list[i] = h
    if not UpdateProcThreadAttribute(
        startup_info.lpAttributeList,
        0,  # reserved and must be 0
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.byref(handles_list),
        ctypes.sizeof(handles_list),
        None,  # reserved and must be null
        None,  # reserved and must be null
    ):
        raise ctypes.WinError()
    return handles_list


class PopenWindowsAsUser(Popen):
    """Class to run a process as another user on Windows.
    Derived from Popen, it defines the _execute_child() method to call CreateProcessWithLogonW.
    """

    def __init__(self, user: WindowsSessionUser, *args: Any, **kwargs: Any):
        """
        Arguments:
            username (str):  Name of user to run subprocess as
            password (str):  Password for username
            args (Any):  Popen constructor args
            kwargs (Any):  Popen constructor kwargs
            https://docs.python.org/3/library/subprocess.html#popen-constructor
        """
        self.user = user
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
        # CreateProcess* may modify the commandline, so copy it to a mutable buffer
        cmdline = ctypes.create_unicode_buffer(commandline)

        if executable is not None:
            executable = os.fsdecode(executable)

        if cwd is not None:
            cwd = os.fsdecode(cwd)

        # Initialize structures
        si = STARTUPINFO()
        si.cb = ctypes.sizeof(STARTUPINFO)
        pi = PROCESS_INFORMATION()

        use_std_handles = -1 not in (p2cread, c2pwrite, errwrite)
        if use_std_handles:
            si.hStdInput = int(p2cread)
            si.hStdOutput = int(c2pwrite)
            si.hStdError = int(errwrite)
            si.dwFlags |= STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
            # Ensure that the console window is hidden
            si.wShowWindow = SW_HIDE

        sys.audit("subprocess.Popen", executable, args, cwd, env, self.user.user)

        def _merge_environment(
            user_env: ctypes.c_void_p, env: dict[str, Optional[str]]
        ) -> ctypes.c_wchar_p:
            user_env_dict = cast(dict[str, Optional[str]], environment_block_to_dict(user_env))
            # All environment keys are converted to uppercase. This is because while Windows environment variables are case insensitive, the win32 api
            # allows you to store multiple keys of mixed case, for example the win32 api would allow you
            # to set both "PATH" and "Path" as environment variable keys. This leads to undefined behaviour when calling one of the keys.
            user_env_dict = {k.upper(): v for k, v in user_env_dict.items()}
            upper_cased_env = {k.upper(): v for k, v in env.items()}
            user_env_dict.update(**upper_cased_env)
            result = {k: v for k, v in user_env_dict.items() if v is not None}
            return environment_block_from_dict(result)

        env_ptr = ctypes.c_void_p(0)
        try:
            if self.user.password is not None:
                with logon_user_context(self.user.user, self.user.password) as logon_token:
                    env_ptr = environment_block_for_user(logon_token)
                    if env:
                        env_block = _merge_environment(env_ptr, env)
                    else:
                        env_block = env_ptr

                # https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw
                if not CreateProcessWithLogonW(
                    self.user.user,
                    None,  # TODO: Domains not yet supported
                    self.user.password,
                    LOGON_WITH_PROFILE,
                    executable,
                    cmdline,
                    creationflags | CREATE_UNICODE_ENVIRONMENT,
                    env_block,
                    cwd,
                    ctypes.byref(si),
                    ctypes.byref(pi),
                ):
                    # Raises: OSError
                    raise ctypes.WinError()
            elif self.user.logon_token is not None:
                siex = STARTUPINFOEX()
                ctypes.memmove(
                    ctypes.pointer(siex.StartupInfo), ctypes.pointer(si), ctypes.sizeof(STARTUPINFO)
                )
                siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
                creationflags |= EXTENDED_STARTUPINFO_PRESENT

                handles_list: Optional[ctypes.Array] = None
                handles_to_inherit = tuple(int(h) for h in (p2cread, c2pwrite, errwrite) if h != -1)
                try:
                    # Allocate the lpAttributeList array of the STARTUPINFOEX structure so that it
                    # has sufficient space for a single attribute. We only have a single attribute that
                    # we're setting -- namely a PROC_THREAD_ATTRIBUTE_HANDLE_LIST that itself contains a list of handles --
                    # so this is sufficient.
                    siex.allocate_attribute_list(1)

                    # Note: We must ensure that 'handles_list' must persist until the
                    # attribute list is destroyed using DeleteProcThreadAttributeList. We do this by holding on
                    # to a reference to it until after the finally block of this try.
                    handles_list = inherit_handles(  # noqa: F841 # ignore: assigned but not used
                        siex, handles_to_inherit
                    )

                    # From https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw
                    # If the lpEnvironment parameter is NULL, the new process inherits the environment of the calling process.
                    # CreateProcessAsUser does not automatically modify the environment block to include environment variables specific to
                    # the user represented by hToken. For example, the USERNAME and USERDOMAIN variables are inherited from the calling
                    # process if lpEnvironment is NULL. It is your responsibility to prepare the environment block for the new process and
                    # specify it in lpEnvironment.

                    env_ptr = environment_block_for_user(self.user.logon_token)
                    if env:
                        env_block = _merge_environment(env_ptr, env)
                    else:
                        env_block = env_ptr

                    if not CreateProcessAsUserW(
                        self.user.logon_token,
                        executable,
                        cmdline,
                        None,
                        None,
                        True,
                        creationflags | CREATE_UNICODE_ENVIRONMENT,
                        env_block,
                        cwd,
                        ctypes.byref(siex),
                        ctypes.byref(pi),
                    ):
                        # Raises: OSError
                        raise ctypes.WinError()
                finally:
                    siex.deallocate_attribute_list()
            else:
                raise NotImplementedError("Unexpected case for WindowsSessionUser properties")
        finally:
            if env_ptr.value is not None:
                DestroyEnvironmentBlock(env_ptr)
            # Child is launched. Close the parent's copy of those pipe
            # handles that only the child should have open.
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

        # Retain the process handle, but close the thread handle
        CloseHandle(pi.hThread)

        self._child_created = True
        self.pid = pi.dwProcessId
        self._handle = Handle(pi.hProcess)
