# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
from typing import Optional, Tuple, Union
from abc import ABC, abstractmethod
from ctypes.wintypes import HANDLE

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
    """Specific os-user identity to run a Session as under Windows."""

    # Background to know:
    #  - What's a "Session" in Windows?
    #     - https://techcommunity.microsoft.com/t5/ask-the-performance-team/sessions-desktops-and-windows-stations/ba-p/372473
    #     - tl;dr - A security environment encapsulating processes and system objects for a logon.
    #  - Session0 in Windows is special. It's where all os services run.
    #     - https://techcommunity.microsoft.com/t5/ask-the-performance-team/application-compatibility-session-0-isolation/ba-p/372361
    #  - The method for creating an impersonated process seems to differ based on whether you're in session 0 or not.
    #     - I suspect that this is a process privilege thing, but have been unable (so far) to figure out what collection of
    #       privileges allow the Session 0 impersonation code to run successfully outside of Session 0.
    #     - In Session != 0:
    #        - Can use win32 API "CreateProcessWithLogon" which takes a username+password, and runs the process as that user.
    #     - In Session = 0:
    #        - CreateProcessWithLogon does not work; at all.
    #        - Must use CreateProcessAsUser or CreateProcessWithToken. We got AsUser working, so we use that.
    #        - CreateProcessAsUser/Token both require a "logon token" rather than a user+pass.
    #          - A token is just an OS object that represents something; a logon in this case.
    #        - Use LogonUserW (or related APIs) to create a logon token.
    #        - We must also explicitly load the user's profile into the logon token using win32api LoadUserProfile
    #  - A process can have only one logon token for each user; trying to create a second when you already have one you
    #    will get a "A required privilege is not held by the client" when you try to LoadUserProfile in to that new token.
    #     - This might be actually be an error that you cannot LoadUserProfile when a token lives that has already loaded the
    #       profile.
    #  - A process that creates a logon token, and loads a profile, is must both: UnloadUserProfile and Close the token.
    # Much more nuanced detail: https://github.com/ddneilson/Win32Impersonation

    # Proposed behavior:
    #  If running in Session != 0 (i.e. interactive logon):
    #    - Token to __init__ not permitted
    #    - If not process user, then password required.
    #    - If process user, then password forbidden
    #    - In LoggingSubprocess:
    #       - If user != process user: use CreateProcessWithLogon
    #       - Else, use standard Popen
    #  Else; running in Session0 (i.e. Within an OS Service)
    #    - If not process user, then exactly one of password or token required.
    #    - If process user, then password & token forbidden.
    #    - If token is given, then just use it
    #       - Assume the caller has correctly constructed it, and loaded profile.
    #    - Else; password is given:
    #       - We retain an internal cache mapping user -> logon token
    #          - WeakRefValue dictionary.
    #          - Each 'value' is an object that has a __del__ method that will unload profile & close token
    #       - Lookup the user in the cache; 
    #          - if exists, then embed a reference to it in this instance
    #          - if not exists, then create a new logon w/ profile load and cache it.
    #       - In LogggingSubprocess:
    #          - If user has a token; then use CreateProcessAsUser to run the subprocess
    #          - Else, use standard Popen
    #       - Documentation to say either always use a token or never use a token; don't mix & match.
    #       - Recommend that consumers maintain their own cache of WindowsSessionUser objects or logon tokens if they
    #         want to avoid doing redoing logon + profile loads during the run of the program 
    #         (e.g. roaming profiles won't initially be supported, but may be a significant load time when supported)
    #      
    #
    # Alternatives:
    #  - Which win32 APIs we use when is not up for debate; we're limited in what APIs are available when
    #    to create processes.
    #  - 1) Create a separate `WindowsSessionTokenUser` that takes user+group+logon_token
    #       - This class `WindowsSessionUser` would remain unmodified by this PR
    #       - Suboption A)
    #          - Caller is responsible for creating the logon token, and implementing the code to create it.
    #       - Suboption B)
    #          - Caller is responsible for creating the logon token, but we provide logon_user/logout_user
    #            helper functions in our public API to assist. Caller is responsible for the lifecycle of
    #            tokens.
    #       - Suboption C)
    #          - Caller is responsible for creating the logon token, but we provide a logon-token cache
    #            as described in the proposed solution.
    #       - Suboption D)
    #          - As (C) but the interface to the token-cache is a method on WindowsSessionUser.
    #             - Caller creates a WindowsSessionUser with the password
    #             - Then caller calls WindowsSessionUser.create_token_user() to get a WindowsSessionTokenUser
    #               instance with a cached logon token.
    #      - This alternative, and all suboptions, discarded as it forces the caller to be aware of the nuances
    #        of impersonation in win32 API. i.e. They have to know that being in Session 0 means that the
    #        WindowsSessionUser instance isn't usable.
    #
    # TBD - Errors. I don't want pywin32 exceptions or ctypes.WinError exceptions to be part of our contract;
    #       that bleeds the abstraction around what interface we're using to run processes, and makes it a breaking
    #       change to change an internal implementation detail if we did surface those exceptions.

    __slots__ = ("user", "group", "password")

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
        self,
        user: str,
        *,
        password: Optional[str] = None,
        group: Optional[str] = None,
        logon_token: Optional[HANDLE] = None,
    ) -> None:
        """
        Arguments:
            user (str):
                User name of the identity to run the Session's subprocesses under.
                This can be either a plain username for a local user, a domain username in down-level logon form,
                or a domain's UPN.
                ex: localUser, domain\\domainUser, domainUser@domain.com

            group (Optional[str]):
                Group name of the identity to run the Session's subprocesses under.
                This can be just a group name for a local group, or a domain group in down-level format.
                ex: localGroup, domain\\domainGroup
                Defaults to the username if not provided.

            password (Optional[str]):
                Password of the identity to run the Session's subprocess under. This argument is
                mutually-exclusive with the "logon_token" argument.

            logon_token (Optional[ctypes.wintypes.HANDLE]):
                Windows logon handle for the target user. This argument is mutually-exclusive with the
                "password" argument.
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
            # TODO: Add exception if given a token; same user doesn't use tokens.
        else:
            # Note: "" is allowed as that may actually be the password for the user.
            if password is None:
                raise RuntimeError("Must supply a password. User is not the process owner.")
            self._validate_username_password(user, domain, password)
            # TODO:
            #  If running outside Session0:
            #     - raise exception if given a token; workflow not supported.
            #  
            # If we're running within Session0

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
