# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from ._os_checker import is_posix, is_windows

if is_posix():
    import grp

if is_windows():
    import win32api
    import win32security

import re

from typing import Optional

__all__ = ("PosixSessionUser", "SessionUser")


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
    This can be just a username for a local user, a domain user's UPN, or a domain user in down-level format.
    ex: localUser, domainuser@domain.com, domain\\domainUser
    """

    group: str
    """
    Group name of the identity to run the Session's subprocesses under.
    This can be just a group name for a local group, or a domain group in down-level format.
    ex: localGroup, domain\\domainGroup
    """

    @staticmethod
    def is_user_upn_format(user):
        """
        Returns true if the provided user is in UPN form, otherwise False.
        Arguments:
            user (str): The user. This can be in UPN form, domain\\username, or just username
        """
        upn_format_regex = (
            '^(?!\\.)[^\\s\\\\/:*?"<>|]{1,15}\\@[^\\s\\@\\\\/:*?"<>|]+\\.[^\\s\\@\\\\/:*?"<>|]+'
        )
        return re.search(upn_format_regex, user) is not None

    @staticmethod
    def try_convert_upn(user):
        """
        Returns the user in down-level logon format (domain\\username) if provided in UPN format.
        Otherwise, returns user unmodified.
        Arguments:
            user (str): The user. This can be in UPN form, down-level logon, or just username
        """
        if WindowsSessionUser.is_user_upn_format(user):
            return win32security.TranslateName(
                user, win32api.NameUserPrincipal, win32api.NameSamCompatible
            )
        else:
            return user

    def __init__(self, user: str, *, group: str) -> None:
        """
        Arguments:
            user (str): The user
            group (str): The group
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")

        self.group = group

        self.user = self.try_convert_upn(user)
