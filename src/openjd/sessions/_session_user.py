# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from ._os_checker import is_posix, is_windows

if is_posix():
    import grp

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
    """User name of the identity to run the Session's subprocesses under.
    """

    # TODO: The group will be only used for directory permission.
    #  Need to revisit this when implementing permission setting.
    group: Optional[str]
    """Group name of the identity to run the Session's subprocesses under.
    """

    def __init__(self, user: str, *, group: Optional[str] = None) -> None:
        """
        Arguments:
            user (str): The user
            group (Optional[str]): The group. Defaults to the name of this
                process' effective group.
        """
        if not is_windows():
            raise RuntimeError("Only available on Windows systems.")
        self.user = user
        self.group = group
