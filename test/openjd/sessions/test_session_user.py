# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
from openjd.sessions._session_user import BadCredentialsException
from openjd.sessions._os_checker import is_windows

from unittest.mock import patch

import pytest


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user",
        ["userA", "domain\\userA"],
    )
    @patch("openjd.sessions._session_user.WindowsSessionUser._validate_username_password")
    @patch(
        "openjd.sessions._session_user.WindowsSessionUser.is_process_user",
        return_value=False,
    )
    def test_user_not_converted(self, mock_is_process_user, mock_validate_username, user):
        windows_session_user = WindowsSessionUser(
            user,
            password="password",
            group="test_group",
        )

        assert windows_session_user.user == user

    def test_no_password_impersonation_throws_exception(self):
        with pytest.raises(
            RuntimeError,
            match="Must supply a password. User is not the process owner.",
        ):
            WindowsSessionUser("nonexistent_user", group="test_group")

    def test_incorrect_credential(self):
        with pytest.raises(
            BadCredentialsException,
            match="The username or password is incorrect.",
        ):
            WindowsSessionUser("nonexistent_user", password="abc")

    def test_split_domain_and_username(self):
        domain, username = WindowsSessionUser._split_domain_and_username("domain\\user")
        assert domain == "domain"
        assert username == "user"
