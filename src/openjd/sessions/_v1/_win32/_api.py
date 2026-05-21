# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
import ctypes
from ctypes.wintypes import (
    BOOL,
    DWORD,
    HANDLE,
    LONG,
    LPCWSTR,
    LPDWORD,
    LPVOID,
    LPWSTR,
    PBYTE,
    PDWORD,
    PHANDLE,
    ULONG,
    WORD,
)
from ctypes import POINTER, WinError, byref, c_byte, c_size_t, c_void_p, pointer  # type: ignore
from collections.abc import Sequence

# This assertion short-circuits mypy from type checking this module on platforms other than Windows
# https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
assert sys.platform == "win32"

# =======================
# Constants
# =======================

# Ref: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithtokenw
LOGON_WITH_PROFILE = 0x00000001
LOGON_NETCREDENTIALS_ONLY = 0x00000002

# STARTINFO Flags (ref: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfoa)
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100

# ShowWindow flags (ref: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow)
SW_HIDE = 0

# TOKEN_PRIVILEGE attributes (ref: https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_privileges)
SE_PRIVILEGE_ENABLED_BY_DEFAULT = 0x00000001
SE_PRIVILEGE_ENABLED = 0x00000002
SE_PRIVILEGE_REMOVED = 0x00000004
SE_PRIVILEGE_USED_FOR_ACCESS = 0x80000000

# Ref: https://learn.microsoft.com/en-us/windows/win32/secauthz/privilege-constants
SE_BACKUP_NAME = "SeBackupPrivilege"
SE_RESTORE_NAME = "SeRestorePrivilege"
SE_INCREASE_QUOTA_NAME = "SeIncreaseQuotaPrivilege"
SE_ASSIGNPRIMARYTOKEN_NAME = "SeAssignPrimaryTokenPrivilege"
SE_TCB_NAME = "SeTcbPrivilege"

# Constant values (ref: https://learn.microsoft.com/en-us/windows/win32/secauthn/logonuserexexw)
LOGON32_PROVIDER_DEFAULT = 0
LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_NETWORK = 3
LOGON32_LOGON_BATCH = 4
LOGON32_LOGON_SERVICE = 5
LOGON32_LOGON_NETWORK_CLEARTEXT = 8

# Prevents displaying of messages
PI_NOUI = 0x00000001

# Values of the TOKEN_TYPE enumeration
# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_type
TOKEN_TYPE_PRIMARY = 1
TOKEN_TYPE_IMPERSONATION = 2

# Values of the SECURITY_IMPERSONATION_LEVEL enumeration (SIL = "Security Impersonation Level")
# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-security_impersonation_level
SIL_ANONYMOUS = 0
SIL_IDENTIFICATION = 1
SIL_IMPERSONATION = 2
SIL_DELEGATION = 3

STANDARD_RIGHTS_REQUIRED = 0x0F0000
STANDARD_RIGHTS_READ = 0x020000
STANDARD_RIGHTS_WRITE = STANDARD_RIGHTS_READ
STANDARD_RIGHTS_EXECUTE = STANDARD_RIGHTS_READ
STANDARD_RIGHTS_ALL = 0x1F0000

# Token access privileges (ref: https://learn.microsoft.com/en-us/windows/win32/secauthz/access-rights-for-access-token-objects)
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_IMPERSONATE = 0x0004
TOKEN_QUERY = 0x0008
TOKEN_QUERY_SOURCE = 0x0010
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_GROUPS = 0x0040
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
TOKEN_READ = STANDARD_RIGHTS_READ | TOKEN_QUERY
TOKEN_WRITE = (
    STANDARD_RIGHTS_WRITE | TOKEN_ADJUST_PRIVILEGES | TOKEN_ADJUST_GROUPS | TOKEN_ADJUST_DEFAULT
)
TOKEN_ALL_ACCESS = (
    STANDARD_RIGHTS_ALL
    | TOKEN_ASSIGN_PRIMARY
    | TOKEN_DUPLICATE
    | TOKEN_IMPERSONATE
    | TOKEN_QUERY
    | TOKEN_QUERY_SOURCE
    | TOKEN_ADJUST_PRIVILEGES
    | TOKEN_ADJUST_GROUPS
    | TOKEN_ADJUST_DEFAULT
    | TOKEN_ADJUST_SESSIONID
)

# From https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_information_class
TokenPrivileges = 3
TokenSecurityAttributes = 39

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002

# =======================
# Structures/Types
# =======================

SIZE_T = c_size_t
PSIZE_T = POINTER(SIZE_T)


# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/ns-processthreadsapi-startupinfow
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
        ("lpReserved2", PBYTE),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


PPROC_THREAD_ATTRIBUTE_LIST = c_void_p
LPPROC_THREAD_ATTRIBUTE_LIST = PPROC_THREAD_ATTRIBUTE_LIST


# https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-startupinfoexw
class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", PPROC_THREAD_ATTRIBUTE_LIST)]

    def allocate_attribute_list(self, num_attributes: int) -> None:
        """Allocate a buffer to lpAttributeList such that it can hold information for
        'num_attributes' attributes.
        Note: You must call 'deallocate_attribute_list()' when done with this structure if you call this;
           that will ensure that the additional allocations that the OS has made are deallocated.
        """
        # As per https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-initializeprocthreadattributelist#remarks
        # First we call InitializeProcThreadAttributeList with an null attribute list,
        # and it'll tell us how large of a buffer lpAttributeList needs to be.
        # This will always return False, so we don't check return code.
        lp_size = SIZE_T(0)
        InitializeProcThreadAttributeList(
            None, num_attributes, 0, byref(lp_size)  # reserved, and must be 0
        )

        # Allocate the desired buffer
        buffer = (c_byte * lp_size.value)()
        self.lpAttributeList = ctypes.cast(pointer(buffer), c_void_p)

        # Second call to actually initialize the buffer
        if not InitializeProcThreadAttributeList(
            self.lpAttributeList, num_attributes, 0, byref(lp_size)  # reserved, and must be 0
        ):
            raise WinError()

    def deallocate_attribute_list(self) -> None:
        DeleteProcThreadAttributeList(self.lpAttributeList)


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
        ("hProfile", HANDLE),
    ]


# https://learn.microsoft.com/en-us/windows/win32/api/wtypesbase/ns-wtypesbase-security_attributes
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", BOOL)]


# https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-luid
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ULONG), ("HighPart", LONG)]


# https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-luid_and_attributes
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
        malloc_size_in_bytes = ctypes.sizeof(TOKEN_PRIVILEGES) + length * ctypes.sizeof(
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

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-deleteprocthreadattributelist
kernel32.DeleteProcThreadAttributeList.restype = None
kernel32.DeleteProcThreadAttributeList.argtypes = [
    LPPROC_THREAD_ATTRIBUTE_LIST,  # [in, out] lpAttributeList
]

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getcurrentprocess
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.GetCurrentProcess.argtypes = []

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getcurrentprocessid
kernel32.GetCurrentProcessId.restype = DWORD
kernel32.GetCurrentProcessId.argtypes = []

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-initializeprocthreadattributelist
kernel32.InitializeProcThreadAttributeList.restype = BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [
    LPPROC_THREAD_ATTRIBUTE_LIST,  # [out, optional] lpAttributeList
    DWORD,  # [in] dwAttributeCount
    DWORD,  # [in] dwFlags
    PSIZE_T,  # [in,out] lpSize
]

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
kernel32.UpdateProcThreadAttribute.restype = BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    LPPROC_THREAD_ATTRIBUTE_LIST,  # [in,out] lpAttributeList
    DWORD,  # [in] dwFlags
    SIZE_T,  # [in] Attribute (note: Pointer-sized integer; not an actual pointer)
    c_void_p,  # [in] lpValue
    SIZE_T,  # [in] cbSize
    c_void_p,  # [out, optional] lpPreviousValue
    PSIZE_T,  # [in, optional] lpReturnSize
]

# https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-processidtosessionid
kernel32.ProcessIdToSessionId.restype = BOOL
kernel32.ProcessIdToSessionId.argtypes = [
    DWORD,  # [in] dwProcessId
    PDWORD,  # [out] pSessionId
]

# exports:
CloseHandle = kernel32.CloseHandle
DeleteProcThreadAttributeList = kernel32.DeleteProcThreadAttributeList
GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcessId = kernel32.GetCurrentProcessId
ProcessIdToSessionId = kernel32.ProcessIdToSessionId
InitializeProcThreadAttributeList = kernel32.InitializeProcThreadAttributeList
UpdateProcThreadAttribute = kernel32.UpdateProcThreadAttribute

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
    PDWORD,  # [out, optional] ReturnLength
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
    POINTER(STARTUPINFOEX),  # [in] lpStartupInfo
    POINTER(PROCESS_INFORMATION),  # [out] lpProcessInformation
]

# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw
advapi32.CreateProcessWithLogonW.restype = BOOL
advapi32.CreateProcessWithLogonW.argtypes = [
    LPCWSTR,  # [in] lpUsername
    LPCWSTR,  # [in, optional] lpDomain
    LPCWSTR,  # [in] lpPassword
    DWORD,  # [in] dwLogonFlags
    LPCWSTR,  # [in, optional] lpApplicationName
    LPWSTR,  # [in, out, optional] lpCommandLine
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
    PDWORD,  # [out] ReturnLength
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
    PHANDLE,  # [out] TokenHandle
]


# exports:
AdjustTokenPrivileges = advapi32.AdjustTokenPrivileges
CreateProcessAsUserW = advapi32.CreateProcessAsUserW
CreateProcessWithLogonW = advapi32.CreateProcessWithLogonW
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
    ctypes.c_void_p,  # [in] lpEnvironment
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
