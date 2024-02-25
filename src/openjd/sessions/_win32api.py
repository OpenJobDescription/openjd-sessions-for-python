# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import ctypes

from ctypes.wintypes import (
    BOOL,
    BYTE,
    DWORD,
    HANDLE,
    LONG,
    LPCWSTR,
    LPDWORD,
    LPVOID,
    LPWSTR,
    PHANDLE,
    ULONG,
    WORD,
)
from ctypes import POINTER
from collections.abc import Sequence

# =======================
# Constants
# =======================

LOGON_WITH_PROFILE = 0x00000001

# STARTINFO Flags (ref: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfoa)
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100

# TOKEN_PRIVILEGE attributes (ref: https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_privileges)
SE_PRIVILEGE_ENABLED_BY_DEFAULT = 0x00000001
SE_PRIVILEGE_ENABLED = 0x00000002
SE_PRIVILEGE_REMOVED = 0x00000004
SE_PRIVILEGE_USED_FOR_ACCESS = 0x80000000

SE_BACKUP_NAME = "SeBackupPrivilege"
SE_RESTORE_NAME = "SeRestorePrivilege"
SE_INCREASE_QUOTA_NAME = "SeIncreaseQuotaPrivilege"
SE_ASSIGNPRIMARYTOKEN_NAME = "SeAssignPrimaryTokenPrivilege"

TOKEN_ADJUST_PRIVILEGES = 0x00000020

# Constant values (ref: https://learn.microsoft.com/en-us/windows/win32/secauthn/logonuserexexw)
LOGON32_PROVIDER_DEFAULT = 0
LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_NETWORK = 3
LOGON32_LOGON_BATCH = 4
LOGON32_LOGON_SERVICE = 5
LOGON32_LOGON_NETWORK_CLEARTEXT = 8

# Prevents displaying of messages
PI_NOUI = 0x00000001

# From https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_information_class
TokenPrivileges = 3
TokenSecurityAttributes = 39

# =======================
# Structures
# =======================


# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfoa
class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", WORD),
        ("cbReserved2", WORD),
        ("lpReserved2", POINTER(BYTE)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-process_information
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


# https://learn.microsoft.com/en-us/windows/win32/api/profinfo/ns-profinfo-profileinfoa
class PROFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("dwFlags", DWORD),
        ("lpUserName", LPWSTR),
        ("lpProfilePath", LPWSTR),
        ("lpDefaultPath", LPWSTR),
        ("lpServerName", LPWSTR),
        ("lpPolicyPath", LPWSTR),
        ("hprofile", HANDLE),
    ]


# https://learn.microsoft.com/en-us/windows/win32/api/wtypesbase/ns-wtypesbase-security_attributes
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", BOOL)]


# https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-luid
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ULONG), ("HighPart", LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_privileges
class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", DWORD),
        # Note: To use
        #   ctypes.cast(ctypes.byref(self.Privileges), ctypes.POINTER(LUID_AND_ATTRIBUTES * self.PrivilegeCount)).contents
        ("Privileges", LUID_AND_ATTRIBUTES * 0),
    ]

    @staticmethod
    def allocate(length: int) -> "TOKEN_PRIVILEGES":
        malloc_size_in_bytes = ctypes.sizeof(TOKEN_PRIVILEGES) + 2 * ctypes.sizeof(
            LUID_AND_ATTRIBUTES
        )
        malloc_buffer = (ctypes.c_byte * malloc_size_in_bytes)()
        token_privs = ctypes.cast(malloc_buffer, POINTER(TOKEN_PRIVILEGES))[0]
        token_privs.PrivilegeCount = length
        return token_privs

    def privileges_array(self) -> Sequence[LUID_AND_ATTRIBUTES]:
        return ctypes.cast(
            ctypes.byref(self.Privileges), ctypes.POINTER(LUID_AND_ATTRIBUTES * self.PrivilegeCount)
        ).contents


# =======================
# APIs
# =======================

# ---------
# From: kernel32.dll
# ---------
kernel32 = ctypes.WinDLL("kernel32")

# https://learn.microsoft.com/en-us/windows/win32/api/handleapi/nf-handleapi-closehandle
kernel32.CloseHandle.restype = BOOL
kernel32.CloseHandle.argtypes = [HANDLE]  # [in] hObject

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getcurrentprocess
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.GetCurrentProcess.argtypes = []

# exports:
CloseHandle = kernel32.CloseHandle
GetCurrentProcess = kernel32.GetCurrentProcess

# ---------
# From: advapi32.dll
# ---------
advapi32 = ctypes.WinDLL("advapi32")

# https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-adjusttokenprivileges
advapi32.AdjustTokenPrivileges.restype = BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    HANDLE,  # [in] TokenHandle
    BOOL,  # [in] DisableAllPrivileges
    POINTER(TOKEN_PRIVILEGES),  # [in, optional] NewState
    DWORD,  # [in] BufferLength
    POINTER(TOKEN_PRIVILEGES),  # [out, optional] PreviousState
    POINTER(DWORD),  # [out, optional] ReturnLength
]

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw
advapi32.CreateProcessAsUserW.restype = BOOL
advapi32.CreateProcessAsUserW.argtypes = [
    HANDLE,  # [in, optional] hToken
    LPCWSTR,  # [in, optional] lpApplicationName
    LPWSTR,  # [in, out, optional] lpCommandLine
    POINTER(SECURITY_ATTRIBUTES),  # [in, optional] lpProcessAttributes
    POINTER(SECURITY_ATTRIBUTES),  # [in, optional] lpThreadAttributes
    BOOL,  # [in] bInheritHandles
    DWORD,  # [in] dwCreationFlags
    LPVOID,  # [in, optional] lpEnvironment
    LPCWSTR,  # [in, optional] lpCurrentDirectory
    POINTER(STARTUPINFO),  # [in] lpStartupInfo
    POINTER(PROCESS_INFORMATION),  # [out] lpProcessInformation
]

# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithtokenw
advapi32.CreateProcessWithTokenW.restype = BOOL
advapi32.CreateProcessWithTokenW.argtypes = [
    HANDLE,  # [in] hToken
    DWORD,  # [in] dwLogonFlags
    LPCWSTR,  # [in, optional] lpApplicationName
    LPWSTR,  # [in, out, optional] lpCommandLine
    DWORD,  # [in] dwCreationFlags
    LPVOID,  # [in,  optional] lpEnvironment
    LPCWSTR,  # [in, optional]  lpCurrentDirectory
    POINTER(STARTUPINFO),  # [in] lpStartupInfo
    POINTER(PROCESS_INFORMATION),  # [out] lpProcessInformation
]

# https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation
advapi32.GetTokenInformation.restype = BOOL
advapi32.GetTokenInformation.argtypes = [
    HANDLE,  # [in] TokenHandle
    DWORD,  # [in] TokenInformationClass (actually an enum)
    LPVOID,  # [out, optional] TokenInformation
    DWORD,  # [in] TokenInformationLength
    POINTER(DWORD),  # [out] ReturnLength
]

# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-logonuserw
advapi32.LogonUserW.restype = BOOL
advapi32.LogonUserW.argtypes = [
    LPCWSTR,  # [in] lpszUsername
    LPCWSTR,  # [in, optional] lpszDomain
    LPCWSTR,  # [in, optional] lpszPassword
    DWORD,  # [in] dwLogonType
    DWORD,  # [in] dwLogonProvider
    PHANDLE,  # [out] phToken
]

# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-lookupprivilegenamew
advapi32.LookupPrivilegeNameW.restype = BOOL
advapi32.LookupPrivilegeNameW.argtypes = [
    LPCWSTR,  # [in, optional] lpSystemName
    POINTER(LUID),  # [in] lpLuid
    LPWSTR,  # [out] lpName
    LPDWORD,  # [in, out] cchName
]

# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-lookupprivilegevaluew
advapi32.LookupPrivilegeValueW.restype = BOOL
advapi32.LookupPrivilegeValueW.argtypes = [
    LPCWSTR,  # [in, optional] lpSystemName
    LPCWSTR,  # [in] lpName
    POINTER(LUID),  # [out] lpLuid
]

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken
advapi32.OpenProcessToken.restype = BOOL
advapi32.OpenProcessToken.argtypes = [
    HANDLE,  # [in] ProcessHandle,
    DWORD,  # [in] DesiredAccess
    ctypes.POINTER(HANDLE),  # [out] TokenHandle
]


# exports:
AdjustTokenPrivileges = advapi32.AdjustTokenPrivileges
CreateProcessAsUserW = advapi32.CreateProcessAsUserW
CreateProcessWithTokenW = advapi32.CreateProcessWithTokenW
GetTokenInformation = advapi32.GetTokenInformation
LogonUserW = advapi32.LogonUserW
LookupPrivilegeNameW = advapi32.LookupPrivilegeNameW
LookupPrivilegeValueW = advapi32.LookupPrivilegeValueW
OpenProcessToken = advapi32.OpenProcessToken

# ---------
# From: userenv.dll
# ---------
userenv = ctypes.WinDLL("userenv")

# https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createenvironmentblock
userenv.CreateEnvironmentBlock.restype = BOOL
userenv.CreateEnvironmentBlock.argtypes = [
    POINTER(ctypes.c_void_p),  # [in] lpEnvironment
    HANDLE,  # [in, optional] hToken
    BOOL,  # [in] bInherit
]

# # https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-destroyenvironmentblock
userenv.DestroyEnvironmentBlock.restype = BOOL
userenv.DestroyEnvironmentBlock.argtypes = [
    POINTER(ctypes.c_void_p),  # [in] lpEnvironment
]

# https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-expandenvironmentstringsforuserw
userenv.ExpandEnvironmentStringsForUserW.restype = BOOL
userenv.ExpandEnvironmentStringsForUserW.argtypes = [
    HANDLE,  # [in, optional] hToken
    LPCWSTR,  # [in] lpSrc
    LPWSTR,  # [out] lpDest
    DWORD,  # [in] dwSize
]

# https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-loaduserprofilew
userenv.LoadUserProfileW.restype = BOOL
userenv.LoadUserProfileW.argtypes = [
    HANDLE,  # [in] hToken
    POINTER(PROFILEINFO),  # [in, out] lpProfileInfo
]

# https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-unloaduserprofile
userenv.UnloadUserProfile.restype = BOOL
userenv.UnloadUserProfile.argtypes = [
    HANDLE,  # [in] hToken
    HANDLE,  # [in] hProfile
]

# exports:
CreateEnvironmentBlock = userenv.CreateEnvironmentBlock
DestroyEnvironmentBlock = userenv.DestroyEnvironmentBlock
ExpandEnvironmentStringsForUserW = userenv.ExpandEnvironmentStringsForUserW
LoadUserProfileW = userenv.LoadUserProfileW
UnloadUserProfile = userenv.UnloadUserProfile
