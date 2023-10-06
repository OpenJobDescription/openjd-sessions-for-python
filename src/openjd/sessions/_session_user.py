# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from ._os_checker import is_posix, is_windows

if is_posix():
    import grp

if is_windows():
    import win32api
    import win32security
    import win32net
    import win32netcon

import re

from typing import Optional

__all__ = ("PosixSessionUser", "SessionUser", "WindowsSessionUser")


class SessionUser:
    """Base class for holding information on the specific os-user identity to run
    a Session as.
    """

    pass


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


class WindowsSessionUser(SessionUser):
    __slots__ = ("user", "group")
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
    This can be just a group name for a local group, or a domain group in down-level format.
    ex: localGroup, domain\\domainGroup
    """

    @staticmethod
    def is_domain_joined() -> bool:
        """
        Returns true if the machine is joined to a domain, otherwise False.
        """
        _, join_status = win32net.NetGetJoinInformation()
        return join_status != win32netcon.NetSetupUnjoined

    def __init__(self, user: str, *, group: str) -> None:
        """
        Arguments:
            user (str): The user
            group (str): The group
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")

        self.group = group

        if "@" in user and self.is_domain_joined():
            user = win32security.TranslateName(user, win32api.NameUserPrincipal, win32api.NameSamCompatible)

        self.user = user
