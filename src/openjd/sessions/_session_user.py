# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from typing import Optional, Tuple, Union
from abc import ABC, abstractmethod

from ._os_checker import is_posix, is_windows

if is_posix():
    import grp
    import pwd

if is_windows():
    import win32api
    import win32security
    import win32net
    import win32netcon
    import pywintypes
    import winerror
    from win32con import LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT

    from ._win32._helpers import get_process_user

__all__ = (
    "PosixSessionUser",
    "SessionUser",
    "WindowsSessionUser",
    "BadCredentialsException",
)


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
    __slots__ = ("user", "group", "password")
    """Specific os-user identity to run a Session as under Windows."""

    user: str
    """
    User name of the identity to run the Session's subprocesses under.
    This can be either a plain username for a local user or a domain username in down-level logon form
    ex: localUser, domain\\domainUser
    """

    group: str
    """
    Group name of the identity to run the Session's subprocesses under.
    This can be just a group name for a local group, or a domain group in down-level logon form.
    ex: localGroup, domain\\domainGroup
    """

    password: Optional[str]
    """
    Password of the identity to run the Session's subprocess under.
    """

    def __init__(
        self, user: str, *, password: Optional[str] = None, group: Optional[str] = None
    ) -> None:
        """
        Arguments:
            user (str): User name of the identity to run the Session's subprocesses under.
                        This can be either a plain username for a local user, a domain username in down-level logon form,
                        or a domain's UPN.
                        ex: localUser, domain\\domainUser, domainUser@domain.com
            group (Optional[str]): Group name of the identity to run the Session's subprocesses under.
                         This can be just a group name for a local group, or a domain group in down-level format.
                         ex: localGroup, domain\\domainGroup
                         Defaults to the username if not provided.
            password (Optional[str]): Password of the identity to run the Session's subprocess under.
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")

        self.password = password

        if "@" in user and self._is_domain_joined():
            user = win32security.TranslateName(
                user, win32api.NameUserPrincipal, win32api.NameSamCompatible
            )

        self.user = user
        self.group = group if group else user

        domain, username_without_domain = self._split_domain_and_username(user)

        # Note: We allow user to be the process user to support the case of being able to supply
        # the group that the process will run under; differing from the user's default group.
        if self.is_process_user():
            if password is not None:
                raise RuntimeError("User is the process owner. Do not provide a password.")
        else:
            # Note: "" is allowed as that may actually be the password for the user.
            if password is None:
                raise RuntimeError("Must supply a password. User is not the process owner.")
            self._validate_username_password(user, domain, password)

    @staticmethod
    def _is_domain_joined() -> bool:
        """
        Returns True if the machine is joined to a domain, else False.
        """
        _, join_status = win32net.NetGetJoinInformation()
        return join_status != win32netcon.NetSetupUnjoined

    @staticmethod
    def _get_process_user():
        return get_process_user()

    @staticmethod
    def _split_domain_and_username(user_name_with_domain: str) -> Tuple[Optional[str], str]:
        """
        Splits a username with domain into domain and username.

        Args:
            user_name_with_domain:  Username needed to be split.
        Returns:
            tuple[Optional[str], str]: domain and username. domain is None if the username is not a domain username.
        """

        domain = None
        user_name = user_name_with_domain
        if "\\" in user_name_with_domain and WindowsSessionUser._is_domain_joined():
            domain, user_name = user_name_with_domain.split("\\")
        return domain, user_name

    @staticmethod
    def _validate_username_password(
        user_name: str, domain_name: Union[str, None], password: str
    ) -> Optional[bool]:
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
        try:
            handle = win32security.LogonUser(
                user_name,
                domain_name,
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
