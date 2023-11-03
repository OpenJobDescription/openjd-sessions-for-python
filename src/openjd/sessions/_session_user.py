# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from ._os_checker import is_posix, is_windows

if is_posix():
    import grp
    import pwd


if is_windows():
    import win32api
    import win32security
    import win32net
    import win32netcon
    import win32con

from typing import Optional

from abc import ABC, abstractmethod

__all__ = ("PosixSessionUser", "SessionUser", "WindowsSessionUser")


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
    def get_process_user():
        """
        Returns the user name of the user running the current process.
        """
        pass

    def is_process_user(self) -> bool:
        """
        Returns True if the session user is the user running the current process, else False.

        """
        return self.user == self.get_process_user()


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
    def get_process_user():
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

    group: Optional[str]
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
            password (Optional[str]): Password of the identity to run the Session's subprocess under.
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")

        self.group = group
        self.password = password

        if "@" in user and self.is_domain_joined():
            user = win32security.TranslateName(
                user, win32api.NameUserPrincipal, win32api.NameSamCompatible
            )

        self.user = user

        if password is None and not self.is_process_user():
            raise RuntimeError(
                "Without passing a password, WindowsSessionUser's user must match the user running Open Job Description."
            )

    @staticmethod
    def is_domain_joined() -> bool:
        """
        Returns True if the machine is joined to a domain, else False.
        """
        _, join_status = win32net.NetGetJoinInformation()
        return join_status != win32netcon.NetSetupUnjoined

    @staticmethod
    def get_process_user():
        """
        Returns the user name of the user running the current process.
        """
        return win32api.GetUserNameEx(win32con.NameSamCompatible)
