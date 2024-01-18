# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import re

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
    import pywintypes
    import winerror
    from win32con import LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT

from typing import Optional, Tuple, Union

from abc import ABC, abstractmethod

__all__ = (
    "PosixSessionUser",
    "SessionUser",
    "WindowsSessionUser",
    "BadCredentialsException",
    "BadUserNameException",
)


class BadCredentialsException(Exception):
    """Exception raised for incorrect username or password."""

    pass


class BadUserNameException(Exception):
    """Exception raised for incorrect username."""

    pass


class BadDomainNameException(Exception):
    """Exception raised for incorrect Domain Name."""

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

        domain, username_without_domain = WindowsSessionUser.split_domain_and_username(user)

        self.is_valid_username(username_without_domain)
        if domain:
            self.is_valid_domain(domain)

        if password is None and not self.is_process_user():
            raise RuntimeError(
                "Without passing a password, WindowsSessionUser's user must match the user running Open Job Description."
            )

        if password is not None:
            self.validate_username_password(user, domain, password)

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

    @staticmethod
    def split_domain_and_username(user_name_with_domain: str) -> Tuple[Optional[str], str]:
        """
        Splits a username with domain into domain and username.

        Args:
            user_name_with_domain:  Username needed to be split.
        Returns:
            tuple[Optional[str], str]: domain and username. domain is None if the username is not a domain username.
        """

        domain = None
        user_name = user_name_with_domain
        if "\\" in user_name_with_domain and WindowsSessionUser.is_domain_joined():
            domain, user_name = user_name_with_domain.split("\\")
        return domain, user_name

    @staticmethod
    def validate_username_password(
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

    @staticmethod
    def is_valid_username(username: str) -> Optional[bool]:
        """
        Validates a username based on specific rules.
        Reference:
        https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/microsoft-windows-shell-setup-autologon-username#values

        This function checks if the given username adheres to the following criteria:
        1. It must be a string.
        2. Its length should not exceed 256 characters.
        3. It should not contain any restricted characters ("/[]:|<>+=;,?*%@).
        4. It should not be the specific name "NONE".

        Parameters:
            username (str): The username to be validated.

        Returns:
            Optional[bool]: True if the username is valid according to the above rules, False otherwise.

        Raises:
            BadUserNameException: If the username doesn't follow the rule.
        """

        if not isinstance(username, str):
            raise BadUserNameException(
                f"Username should be a string, not {type(username).__name__}."
            )

        if len(username) > 256 or len(username) == 0:
            raise BadUserNameException("Username must have a length between 1 and 256 characters.")

        # Set of restricted characters
        restricted_chars = set('"/[]:|<>+=;,?*%@')

        if restricted_chars.intersection(username):
            raise BadUserNameException(
                f"Username contains restricted characters {restricted_chars.intersection(username)}. "
                f"Allowed characters do not include any of {restricted_chars}."
            )

        if username.upper() == "NONE":
            raise BadUserNameException("Username cannot be 'NONE'.")

        return True

    @staticmethod
    def is_valid_domain(domain: str) -> Optional[bool]:
        """
        Validates the domain name based on specific criteria.
        Reference:
        https://learn.microsoft.com/en-us/troubleshoot/windows-server/identity/naming-conventions-for-computer-domain-site-ou#dns-domain-names

        This function checks if a given domain name adheres to certain standards:
        - The domain name must only contain alphabetic characters (A-Z,a-z), numeric characters (0-9),
          the minus sign (-), and the period (.).
        - The domain name's length must be between 2 and 255 characters.

        Args:
            domain (str): The domain name to validate.

        Returns:
            Optional[bool]: Returns True if the domain name is valid.

        Raises:
            BadDomainNameException: If the domain name contains disallowed characters or does not meet the
            length requirements, this exception is raised with a message detailing the validation issue.
        """

        # Regex pattern to match only allowed characters
        pattern = r"^[A-Za-z0-9\-.]+$"

        # Check if the string matches the pattern
        if not re.match(pattern, domain):
            raise BadDomainNameException(
                f"Domain name '{domain}' contains disallowed characters."
                "alphabetic characters (A-Z), numeric characters (0-9), "
                "the minus sign (-), and the period (.) are only allowed in the domain name."
            )

        if len(domain) > 255 or len(domain) < 2:
            raise BadDomainNameException(
                "Domain name must have a length between 2 and 255 characters."
            )

        return True
