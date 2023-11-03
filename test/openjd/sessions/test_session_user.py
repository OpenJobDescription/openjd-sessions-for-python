# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
from openjd.sessions._os_checker import is_windows

from unittest.mock import patch

import pytest


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user",
        ["userA", "domain\\userA"],
    )
    def test_user_not_converted(self, user):
        with patch(
            "openjd.sessions._session_user.WindowsSessionUser.is_process_user",
            return_value=True,
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
