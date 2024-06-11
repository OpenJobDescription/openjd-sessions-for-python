# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from typing import Optional
from abc import ABC, abstractmethod
from ctypes.wintypes import HANDLE

from ._os_checker import is_posix, is_windows

if is_posix():
    import grp
    import pwd

if is_windows():
    import win32api
    import win32security
    import pywintypes
    import winerror
    from win32con import LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT

    from ._win32._helpers import get_process_user, get_current_process_session_id  # type: ignore

__all__ = (
    "PosixSessionUser",
    "SessionUser",
    "WindowsSessionUser",
    "BadCredentialsException",
)

CURRENT_PROCESS_RUNNING_IN_WINDOWS_SESSION_0: bool
if is_windows():
    CURRENT_PROCESS_RUNNING_IN_WINDOWS_SESSION_0 = 0 == get_current_process_session_id()
else:
    CURRENT_PROCESS_RUNNING_IN_WINDOWS_SESSION_0 = False


class BadCredentialsException(Exception):
    """Exception raised for incorrect username or password."""

    pass


class SessionUser(ABC):
    """Base class for holding information on the specific os-user identity to run
    a Session as.
    """

    user: str
    """
    User name of the identity to run the Session's subprocesses under.
    """

    @staticmethod
    @abstractmethod
    def _get_process_user():
        """
        Returns the user name of the user running the current process.
        """
        pass

    def is_process_user(self) -> bool:
        """
        Returns True if the session user is the user running the current process, else False.

        """
        return self.user == self._get_process_user()


class PosixSessionUser(SessionUser):
    __slots__ = ("user", "group")
    """Specific os-user identity to run a Session as under Linux/macOS."""

    user: str
    """User name of the identity to run the Session's subprocesses under.
    """

    group: str
    """Group name of the identity to run the Session's subprocesses under.
    """

    def __init__(self, user: str, *, group: Optional[str] = None) -> None:
        """
        Arguments:
            user (str): The user
            group (Optional[str]): The group. Defaults to the name of this
                process' effective group.
        """
        if not is_posix():
            raise RuntimeError("Only available on posix systems.")
        self.user = user
        self.group = group if group else grp.getgrgid(os.getegid()).gr_name  # type: ignore

    @staticmethod
    def _get_process_user():
        """
        Returns the user name of the user running the current process.
        """
        return pwd.getpwuid(os.geteuid()).pw_name


class WindowsSessionUser(SessionUser):
    """Specific os-user identity to run a Session as under Windows.

    Note that you must check whether you are running in Windows Session 0 prior to
    creating an instance of this class.
    1. If you're not in Session 0 (i.e. you're in a typical interactive logon via the desktop)
       then you must instantiate this class with a username + password; providing a logon token
       is not allowed at this time.
    2. If you are in Session 0 (i.e. you're running within the context of a Windows Service; this
       includes a logon session obtained by ssh-ing into the host), then you must instantiate this
       class with a username + logon_token; providing a password is not allowed in Session 0. To
       create a logon_token, you will want to look in to the LogonUser family of Win32 system APIs.

    The user provided in this class directly influences the Directory ACL of the Session Working
    Directory that is created. The created directory:
    1. Has Full Control by the owner of the calling process; and
    2. Has Modify access by the provided user.
    The Session working directory will also be set so that all child directories and files
    inherit these permissions.
    """

    __slots__ = ("user", "password", "logon_token")

    user: str
    """
    User name of the identity to run the Session's subprocesses under.
    This can be either a plain username for a local user or a domain username in down-level logon form
    ex: localUser, domain\\domainUser
    """

    password: Optional[str]
    """
    Password of the identity to run the Session's subprocess(es) under.
    Mutually exclusive with: logon_token
    """

    logon_token: Optional[HANDLE]
    """
    A logon token to use to run the Session's subprocess(es) under.
    Mutually exclusive with: password
    """

    def __init__(
        self,
        user: str,
        *,
        password: Optional[str] = None,
        logon_token: Optional[HANDLE] = None,
    ) -> None:
        """
        Arguments:
            user (str):
                User name of the identity to run the Session's subprocesses under.
                This can be either a plain username for a local user, a domain username in down-level logon form,
                or a domain's UPN.
                ex: localUser, domain\\domainUser, domainUser@domain.com

            password (Optional[str]):
                Password of the identity to run the Session's subprocess under. This argument is mutually-exclusive with the
                "logon_token" argument.

            logon_token (Optional[ctypes.wintypes.HANDLE]):
                Windows logon handle for the target user. This argument is mutually-exclusive with the
                "password" argument.
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")

        if password and logon_token:
            raise ValueError('The "password" and "logon_token" arguments are mutually exclusive')

        self.user = user

        # Note: We allow user to be the process user to support the case of being able to supply
        # the group that the process will run under; differing from the user's default group.
        if self.is_process_user():
            if password is not None:
                raise RuntimeError("User is the process owner. Do not provide a password.")
            if logon_token is not None:
                raise RuntimeError("User is the process owner. Do not provide a logon token.")
        else:
            # Note: "" is allowed as that may actually be the password for the user.
            if password is None and logon_token is None:
                raise RuntimeError(
                    "Must supply a password or logon token. User is not the process owner."
                )
            if password is not None:
                if CURRENT_PROCESS_RUNNING_IN_WINDOWS_SESSION_0:
                    raise RuntimeError(
                        (
                            "Must supply a logon_token rather than a password. "
                            "Passwords are not supported when running in Windows Session 0."
                        )
                    )
                self._validate_username_password(user, password)
                self.password = password
                self.logon_token = None
            else:
                self.password = None
                self.logon_token = logon_token

    @staticmethod
    def _get_process_user():
        return get_process_user()

    @staticmethod
    def _validate_username_password(user_name: str, password: str) -> Optional[bool]:
        """
        Validates the username and password against Windows authentication.

        Args:
            user_name (str): The username to be validated.
            domain_name (str): The domain where the user exists. None means current domain.
            password (str): The password to be validated.

        Returns:
            Optional[bool]: True if the credentials are valid

        Raises:
            BadCredentialsException: If the username or password is incorrect.
        """
        if "\\" in user_name:
            domain, username = user_name.split("\\")
        else:
            domain, username = None, user_name
        try:
            handle = win32security.LogonUser(
                username,
                domain,
                password,
                LOGON32_LOGON_INTERACTIVE,
                LOGON32_PROVIDER_DEFAULT,
            )
            win32api.CloseHandle(handle)
            return True
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_LOGON_FAILURE:
                raise BadCredentialsException("The username or password is incorrect.")
            raise
