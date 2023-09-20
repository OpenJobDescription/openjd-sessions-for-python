# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import unittest
from enum import Enum
from unittest.mock import patch
from openjd.sessions._os_checker import is_posix, is_windows, check_os


class OSName(str, Enum):
    POSIX = "posix"
    WINDOWS = "nt"


class TestOSChecker(unittest.TestCase):
    @patch("openjd.sessions._os_checker.os")
    def test_is_posix(self, mock_os):
        mock_os.name = OSName.POSIX
        self.assertTrue(is_posix())

    @patch("openjd.sessions._os_checker.os")
    def test_is_not_posix(self, mock_os):
        mock_os.name = OSName.WINDOWS
        self.assertFalse(is_posix())

    @patch("openjd.sessions._os_checker.os")
    def test_is_windows(self, mock_os):
        mock_os.name = OSName.WINDOWS
        self.assertTrue(is_windows())

    @patch("openjd.sessions._os_checker.os")
    def test_is_not_windows(self, mock_os):
        mock_os.name = OSName.POSIX
        self.assertFalse(is_windows())

    @patch("openjd.sessions._os_checker.os")
    def test_check_os_posix(self, mock_os):
        mock_os.name = OSName.POSIX
        check_os()

    @patch("openjd.sessions._os_checker.os")
    def test_check_os_windows(self, mock_os):
        mock_os.name = OSName.WINDOWS
        check_os()

    @patch("openjd.sessions._os_checker.os")
    def test_check_os_unsupported(self, mock_os):
        mock_os.name = "unsupported_os"
        with self.assertRaises(NotImplementedError) as context:
            check_os()
        self.assertIn("os: unsupported_os is not supported yet.", str(context.exception))
