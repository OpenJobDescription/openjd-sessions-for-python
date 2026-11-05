# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Find the processes holding files open, via the Windows Restart Manager.

Used only to explain a failed cleanup in the log, so every failure here is
raised as OSError for the caller to report, and never stops the cleanup.
"""

import sys
from ctypes import POINTER, Structure, WinDLL, byref, c_int, create_unicode_buffer
from ctypes.wintypes import BOOL, DWORD, FILETIME, LPCWSTR, LPDWORD, LPWSTR, UINT, ULONG, WCHAR
from typing import NamedTuple, Sequence

import psutil

# This assertion short-circuits mypy from type checking this module on platforms other than Windows
# https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
assert sys.platform == "win32"

CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234


class RM_UNIQUE_PROCESS(Structure):
    _fields_ = [("dwProcessId", DWORD), ("ProcessStartTime", FILETIME)]


class RM_PROCESS_INFO(Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", c_int),
        ("AppStatus", ULONG),
        ("TSSessionId", DWORD),
        ("bRestartable", BOOL),
    ]


rstrtmgr = WinDLL("rstrtmgr")

# https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmstartsession
rstrtmgr.RmStartSession.restype = DWORD
rstrtmgr.RmStartSession.argtypes = [LPDWORD, DWORD, LPWSTR]
# https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmregisterresources
rstrtmgr.RmRegisterResources.restype = DWORD
rstrtmgr.RmRegisterResources.argtypes = [
    DWORD,
    UINT,
    POINTER(LPCWSTR),
    UINT,
    POINTER(RM_UNIQUE_PROCESS),
    UINT,
    POINTER(LPCWSTR),
]
# https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmgetlist
rstrtmgr.RmGetList.restype = DWORD
rstrtmgr.RmGetList.argtypes = [
    DWORD,
    POINTER(UINT),
    POINTER(UINT),
    POINTER(RM_PROCESS_INFO),
    LPDWORD,
]
# https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmendsession
rstrtmgr.RmEndSession.restype = DWORD
rstrtmgr.RmEndSession.argtypes = [DWORD]


class FileHolder(NamedTuple):
    pid: int
    name: str


def processes_holding(files: Sequence[str]) -> list[FileHolder]:
    """Returns the processes that hold any of ``files`` open.

    Raises:
        OSError: if the Restart Manager could not be queried.
    """
    if not files:
        return []
    handle = DWORD()
    key = create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    rc = rstrtmgr.RmStartSession(byref(handle), 0, key)
    if rc != ERROR_SUCCESS:
        raise OSError(f"RmStartSession failed ({rc})")
    try:
        return _list_holders(handle, files)
    finally:
        rstrtmgr.RmEndSession(handle)


def _list_holders(handle: DWORD, files: Sequence[str]) -> list[FileHolder]:
    names = (LPCWSTR * len(files))(*files)
    rc = rstrtmgr.RmRegisterResources(handle, len(files), names, 0, None, 0, None)
    if rc != ERROR_SUCCESS:
        raise OSError(f"RmRegisterResources failed ({rc})")
    infos = (RM_PROCESS_INFO * 0)()
    # The list can grow between calls, so ask again a few times on ERROR_MORE_DATA.
    for _ in range(3):
        needed = UINT(0)
        count = UINT(len(infos))
        reasons = DWORD(0)
        rc = rstrtmgr.RmGetList(handle, byref(needed), byref(count), infos, byref(reasons))
        if rc == ERROR_SUCCESS:
            return [_holder_from(infos[i]) for i in range(count.value)]
        if rc != ERROR_MORE_DATA:
            raise OSError(f"RmGetList failed ({rc})")
        infos = (RM_PROCESS_INFO * needed.value)()
    raise OSError("RmGetList kept reporting more processes")


def _holder_from(info: RM_PROCESS_INFO) -> FileHolder:
    pid = int(info.Process.dwProcessId)
    try:
        # The executable's file name, such as UnrealShaderCompileWorker.exe.
        name = psutil.Process(pid).name()
    except psutil.Error:
        name = info.strAppName
    return FileHolder(pid=pid, name=name)
