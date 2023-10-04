# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._session_user import WindowsSessionUser
import pytest


class TestWindowsSessionUser:
    @pytest.mark.parametrize(
        "user,expected_result",
        [
            ("testuser@testdomain.com", True),
            ("test.user@testdomain.com", True),
            ("testuser@test.domain.com", True),
            ("testuser", False),
            ("testdomain\\testuser", False),
            (".\\testuser", False),
            ("testuser@testdomain", False),
            (".testuser@testdomain.com", False),
            ("testuser@testdomain.", False),
        ],
    )
    def test_is_user_upn_format(self, user, expected_result):
        result = WindowsSessionUser.is_user_upn_format(user)

        assert result is expected_result

    @pytest.mark.parametrize("user_in", ["user", "domain\\user"])
    def test_try_convert_upn_doesnt_convert_non_upn(self, user_in):
        user_out = WindowsSessionUser.try_convert_upn(user_in)

        assert user_out == user_in
