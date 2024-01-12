# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
from openjd.sessions._session_user import BadCredentialsException
from openjd.sessions._session_user import BadUserNameException
from openjd.sessions._session_user import BadDomainNameException
from openjd.sessions._os_checker import is_windows

from unittest.mock import patch

import pytest


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user",
        ["userA", "domain\\userA"],
    )
    @patch(
        "openjd.sessions._session_user.WindowsSessionUser.is_process_user",
        return_value=True,
    )
    @patch(
        "openjd.sessions._session_user.WindowsSessionUser.is_valid_username",
        return_value=True,
    )
    @patch(
        "openjd.sessions._session_user.WindowsSessionUser.validate_username_password",
        return_value=True,
    )
    def test_user_not_converted(
        self, mock_is_process_user, mock_is_valid_username, mock_validate_username_password, user
    ):
        windows_session_user = WindowsSessionUser(
            user,
            password="password",
            group="test_group",
        )

        assert windows_session_user.user == user

    def test_no_password_impersonation_throws_exception(self):
        with pytest.raises(
            RuntimeError,
            match="Without passing a password, WindowsSessionUser's user must match the user running Open Job Description.",
        ):
            WindowsSessionUser("nonexistent_user", group="test_group")

    def test_incorrect_credential(self):
        with pytest.raises(
            BadCredentialsException,
            match="The username or password is incorrect.",
        ):
            WindowsSessionUser("nonexistent_user", password="abc")

    def test_incorrect_username(self):
        with pytest.raises(
            BadUserNameException,
            match="Username contains restricted characters ",
        ):
            WindowsSessionUser("?", password="abc")

    def test_split_domain_and_username(self):
        domain, username = WindowsSessionUser.split_domain_and_username("domain\\user")
        assert domain == "domain"
        assert username == "user"


class TestIsValidUsername:
    def test_valid_username(self):
        assert WindowsSessionUser.is_valid_username("valid_username")

    def test_non_string_username(self):
        with pytest.raises(BadUserNameException, match="Username should be a string, not"):
            WindowsSessionUser.is_valid_username(123)  # type: ignore

    def test_too_long_username(self):
        with pytest.raises(
            BadUserNameException, match="Username must have a length between 1 and 256 characters"
        ):
            WindowsSessionUser.is_valid_username("a" * 257)

    def test_empty_username(self):
        with pytest.raises(
            BadUserNameException, match="Username must have a length between 1 and 256 characters"
        ):
            WindowsSessionUser.is_valid_username("")

    def test_username_with_restricted_chars(self):
        with pytest.raises(BadUserNameException, match="Username contains restricted characters"):
            WindowsSessionUser.is_valid_username("/username")

    def test_username_none(self):
        with pytest.raises(BadUserNameException, match="Username cannot be 'NONE'"):
            WindowsSessionUser.is_valid_username("NONE")


class TestIsValidDomainName:
    def test_valid_domain(self):
        assert WindowsSessionUser.is_valid_domain("example.com")
        assert WindowsSessionUser.is_valid_domain("sub-domain.domain.org")
        assert WindowsSessionUser.is_valid_domain("example")
        assert WindowsSessionUser.is_valid_domain("domain1")
        assert WindowsSessionUser.is_valid_domain("SUBDOMAIN.DOMAIN")

    def test_domain_with_disallowed_characters(self):
        with pytest.raises(
            BadDomainNameException,
            match="Domain name 'example.com!' contains disallowed characters",
        ):
            WindowsSessionUser.is_valid_domain("example.com!")

    def test_domain_too_short(self):
        with pytest.raises(
            BadDomainNameException,
            match="Domain name must have a length between 2 and 255 characters.",
        ):
            WindowsSessionUser.is_valid_domain("e")

    def test_domain_too_long(self):
        long_domain = "a" * 256
        with pytest.raises(
            BadDomainNameException,
            match="Domain name must have a length between 2 and 255 characters.",
        ):
            WindowsSessionUser.is_valid_domain(long_domain)
