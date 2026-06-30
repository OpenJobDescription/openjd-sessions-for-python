# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys

# This assertion short-circuits mypy from type checking this module on platforms other than Windows
# https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
assert sys.platform == "win32"

import win32api
import win32con
from ctypes.wintypes import DWORD, HANDLE
from ctypes import (
    WinError,
    byref,
    cast,
    c_void_p,
    c_wchar,
    c_wchar_p,
    sizeof,
)
from contextlib import contextmanager
from typing import Generator, Optional

from ._api import (
    # Constants
    LOGON32_LOGON_INTERACTIVE,
    LOGON32_PROVIDER_DEFAULT,
    PI_NOUI,
    PROFILEINFO,
    # Functions
    CloseHandle,
    CreateEnvironmentBlock,
    DestroyEnvironmentBlock,
    GetCurrentProcessId,
    LogonUserW,
    ProcessIdToSessionId,
    LoadUserProfileW,
    UnloadUserProfile,
)


def get_process_user():
    """
    Returns the user name of the user running the current process.
    """
    return win32api.GetUserNameEx(win32con.NameSamCompatible)


def get_current_process_session_id() -> int:
    """
    Finds the Session ID of the current process, and returns it.
    """
    proc_id = GetCurrentProcessId()
    session_id = DWORD(0)
    # Ignore the return value; will only fail if given a bad
    # process id, and that's clearly impossible here.
    ProcessIdToSessionId(proc_id, byref(session_id))
    return session_id.value


def logon_user(username: str, password: str) -> HANDLE:
    """
    Attempt to logon as the given username & password.
    Return a HANDLE to a logon_token.

    Note:
      The caller *MUST* call CloseHandle on the returned value when done with it.
      Handles are not automatically garbage collected.

    Raises:
        OSError - If an error is encountered.
    """
    hToken = HANDLE(0)
    if not LogonUserW(
        username,
        None,  # TODO - domain handling??
        password,
        LOGON32_LOGON_INTERACTIVE,
        LOGON32_PROVIDER_DEFAULT,
        byref(hToken),
    ):
        raise WinError()

    return hToken


@contextmanager
def logon_user_context(username: str, password: str) -> Generator[HANDLE, None, None]:
    """
    A context manager wrapper around logon_user(). This will automatically
    Close the logon_token when the context manager is exited.
    """
    hToken: Optional[HANDLE] = None
    try:
        hToken = logon_user(username, password)
        yield hToken
    finally:
        if hToken is not None and not CloseHandle(hToken):
            raise WinError()


def environment_block_for_user(logon_token: HANDLE) -> c_void_p:
    """
    Create an Environment Block for a given logon_token and return it.
    Per https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createenvironmentblock
    "The environment block is an array of null-terminated Unicode strings. The list ends with two nulls (\0\0)."

    Returns:
        Pointer to the environment block

    Raises:
        OSError - If there is an error creating the block

    Notes:
     1) The returned block *MUST* be deallocated with DestroyEnvironmentBlock when done
     2) Destroying an environment block while it is in use (e.g. while the process it was passed
        to is still running) WILL result in a hard to debug crash in ntdll.dll. So, don't do that!
    """
    environment = c_void_p()
    if not CreateEnvironmentBlock(byref(environment), logon_token, False):
        raise WinError()
    return environment


@contextmanager
def environment_block_for_user_context(logon_token: HANDLE) -> Generator[c_void_p, None, None]:
    """As environment_block_for_user, but as a context manager. This will automatically
    Destroy the environment block when exiting the context.
    """
    lp_environment: Optional[c_void_p] = None
    try:
        lp_environment = environment_block_for_user(logon_token)
        yield lp_environment
    finally:
        if lp_environment is not None and not DestroyEnvironmentBlock(lp_environment):
            raise WinError()


def environment_block_to_dict(block: c_void_p) -> dict[str, str]:
    """Converts an environment block as returned from CreateEnvironmentBlock to a Python dict of key/value strings.

    An environment block is a void pointer. The pointer points to a sequence of strings. Each string is terminated with a
    null character. The final string is terminated by an additional null character.
    """
    assert block.value is not None
    w_char_size = sizeof(c_wchar)
    cur: int = block.value
    key_val_str = cast(cur, c_wchar_p).value
    env: dict[str, str] = {}
    while key_val_str:
        key, val = key_val_str.split("=", maxsplit=1)
        env[key] = val
        # Advance pointer by string length plus a null character
        cur += (len(key_val_str) + 1) * w_char_size
        key_val_str = cast(cur, c_wchar_p).value

    return env


def environment_block_from_dict(env: dict[str, str]) -> c_wchar_p:
    """Converts a Python dictionary representation of an environment into a character buffer as expected by the
    lpEnvironment argument to the CreateProcess* family of win32 functions.

    Note: The returned c_char_p is pointing to the internal contents of an immutable python string; that is
        to say that it will be garbage collected, and the caller need not worry about deallocating it.
    """
    # Create a string with null-character delimiters between each "key=value" string
    null_delimited = "\0".join(f"{key}={value}" for key, value in env.items())
    # Add a final null-terminator character
    env_block_str = null_delimited + "\0"

    return c_wchar_p(env_block_str)


def load_user_profile(token: HANDLE, username: str) -> PROFILEINFO:
    """
    Load the user profile for the given logon token and user name

    NOTE: The caller *MUST* call unload_user_profile when finished with the user profile

    See: https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-loaduserprofilew
    """
    profile_info = PROFILEINFO()
    profile_info.dwSize = sizeof(PROFILEINFO)
    profile_info.lpUserName = username
    profile_info.dwFlags = PI_NOUI
    profile_info.lpProfilePath = None

    if not LoadUserProfileW(token, byref(profile_info)):
        raise WinError()

    return profile_info


def unload_user_profile(token: HANDLE, profile_info: PROFILEINFO) -> None:
    """
    Unload the user profile for the given token and profile.

    See: https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-unloaduserprofile
    """
    if not UnloadUserProfile(token, profile_info.hProfile):
        raise WinError()
