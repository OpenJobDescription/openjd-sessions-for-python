# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
from openjd.sessions._os_checker import is_windows

import pytest


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user",
        ["userA", "domain\\userA"],
    )
    def test_user_not_converted(self, user):
        windows_session_user = WindowsSessionUser(
            user,
            "password",
            group="test_group",
        )

        assert windows_session_user.user == user
