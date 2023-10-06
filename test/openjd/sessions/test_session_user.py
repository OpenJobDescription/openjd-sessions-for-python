# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
import pytest


class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user",
        ["userA", "domain\\userA"],
    )
    def test_ctor_user_not_converted(self, user):
        windows_session_user = WindowsSessionUser(user, group="test_group")

        assert windows_session_user.user == user
