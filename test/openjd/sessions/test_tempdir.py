# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
import tempfile
from pathlib import Path
from subprocess import DEVNULL, run

from openjd.sessions._os_checker import is_posix, is_windows

if is_posix():
    import grp
    import pwd

if is_windows():
    import win32security

import pytest
from unittest.mock import patch

from openjd.sessions import PosixSessionUser, WindowsSessionUser
from openjd.sessions._tempdir import TempDir

from .conftest import has_posix_disjoint_user, has_posix_target_user


@pytest.mark.skipif(not is_posix(), reason="Posix-specific tests")
class TestTempDirPosix:
    def test_defaults(self) -> None:
        # GIVEN
        tmpdir = Path(tempfile.gettempdir())

        # WHEN
        result = TempDir()

        # THEN
        assert result.path.parent == tmpdir
        assert os.path.exists(result.path)

        statinfo = os.stat(result.path)
        assert statinfo.st_uid == os.getuid()  # type: ignore
        assert statinfo.st_gid == os.getgid()  # type: ignore

        os.rmdir(result.path)


class TestTempDir:
    @pytest.mark.usefixtures("tmp_path")  # Built-in fixture
    def test_given_dir(self, tmp_path: Path) -> None:
        # WHEN
        result = TempDir(dir=tmp_path)

        # THEN
        assert result.path.parent == tmp_path.resolve()
        assert os.path.exists(result.path)

    def test_given_prefix(self) -> None:
        # GIVEN
        tmpdir = Path(tempfile.gettempdir())
        prefix = "testprefix"

        # WHEN
        result = TempDir(prefix=prefix)

        # THEN
        assert result.path.parent == tmpdir.resolve()
        assert result.path.name.startswith(prefix)
        assert os.path.exists(result.path)

        os.rmdir(result.path)

    def test_cleanup(self) -> None:
        # GIVEN
        tmpdir = TempDir()
        open(tmpdir.path / "file.txt", "w").close()

        # WHEN
        tmpdir.cleanup()

        # THEN
        assert not os.path.exists(tmpdir.path)

    def test_no_write_permission(self) -> None:
        # Test that we raise an exception if we don't have permission to create a directory
        # within the given directory.

        # GIVEN
        dir = Path(tempfile.gettempdir()) / "a" / "very" / "unlikely" / "dir" / "to" / "exist"

        # WHEN
        with pytest.raises(RuntimeError):
            TempDir(dir=dir)


@pytest.mark.xfail(not is_windows(), reason="Windows-specific tests")
class TestTempDirWindowsUser:
    FULL_CONTROL_MASK = 2032127

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_user_permits_admins_group(self, mock_user_match):
        # GIVEN
        # Use a builtin group, so we can expect it to exist on any Windows machine
        # The mocked `is_process_user` is only used in WindowsSessionUser for parameter validation
        windows_user = WindowsSessionUser("arbitrary_user", group="Users")

        # WHEN
        tempdir = TempDir(user=windows_user)

        # THEN
        assert self.principal_has_full_control_of_object(str(tempdir.path), "Administrators")

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_user_with_group_permits_group(self, mock_user_match):
        # GIVEN
        # Use a builtin group, so we can expect it to exist on any Windows machine
        windows_user = WindowsSessionUser("arbitrary_user", group="Users")

        # WHEN
        tempdir = TempDir(user=windows_user)

        # THEN
        assert self.principal_has_full_control_of_object(str(tempdir.path), windows_user.group)

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_wrong_group_not_permitted(self, mock_user_match):
        # GIVEN
        # Use a builtin group, so we can expect it to exist on any Windows machine
        windows_user = WindowsSessionUser("arbitrary_user", group="Users")

        # WHEN
        tempdir = TempDir(user=windows_user)

        # THEN
        assert self.principal_has_no_permissions_on_object(str(tempdir.path), "Guests")

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_user_with_group_permits_group_permissions_inherited(self, mock_user_match):
        # GIVEN
        # Use a builtin group, so we can expect it to exist on any Windows machine
        windows_user = WindowsSessionUser("arbitrary_user", group="Users")

        # WHEN
        tempdir = TempDir(user=windows_user)
        os.mkdir(tempdir.path / "child_dir")
        os.mkdir(tempdir.path / "child_dir" / "grandchild_dir")
        open(tempdir.path / "child_file", "a").close()
        open(tempdir.path / "child_dir" / "grandchild_file", "a").close()

        # THEN
        assert self.principal_has_full_control_of_object(str(tempdir.path), windows_user.group)
        assert self.principal_has_full_control_of_object(
            str(tempdir.path / "child_dir"), windows_user.group
        )
        assert self.principal_has_full_control_of_object(
            str(tempdir.path / "child_file"), windows_user.group
        )
        assert self.principal_has_full_control_of_object(
            str(tempdir.path / "child_dir" / "grandchild_dir"), windows_user.group
        )
        assert self.principal_has_full_control_of_object(
            str(tempdir.path / "child_dir" / "grandchild_file"), windows_user.group
        )

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_user_without_group_permits_user(self, mock_user_match):
        # GIVEN
        windows_user = WindowsSessionUser("Guest")

        # WHEN
        tempdir = TempDir(user=windows_user)

        # THEN
        assert self.principal_has_full_control_of_object(str(tempdir.path), "Guest")

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_invalid_windows_group_raises_exception(self, mock_user_match):
        # GIVEN
        windows_user = WindowsSessionUser("Guest", group="nonexistent_group")

        # THEN
        with pytest.raises(RuntimeError, match="Could not change permissions of directory"):
            TempDir(user=windows_user)

    @staticmethod
    def get_aces_for_principal_on_object(object_path: str, principal_name: str):
        """
        Returns a list of access allowed masks and a list of access denied masks for a principal's permissions on an object.
        Access masks for principals other than that specified by principal_name will be excluded from both lists.

        Arguments:
            object_path (str): The path to the object for which the ACE masks will be retrieved.
            principal_name (str): The name of the principal to filter for.

        Returns:
            access_allowed_masks (List[int]): All masks allowing principal_name access to the file.
            access_denied_masks (List[int]): All masks denying principal_name access to the file.
        """
        sd = win32security.GetFileSecurity(object_path, win32security.DACL_SECURITY_INFORMATION)

        dacl = sd.GetSecurityDescriptorDacl()

        principal_to_check_sid, _, _ = win32security.LookupAccountName(None, principal_name)

        access_allowed_masks = []
        access_denied_masks = []

        for i in range(dacl.GetAceCount()):
            ace = dacl.GetAce(i)

            ace_type = ace[0][0]
            access_mask = ace[1]
            ace_principal_sid = ace[2]

            account_name, _, _ = win32security.LookupAccountSid(None, ace_principal_sid)

            if ace_principal_sid == principal_to_check_sid:
                if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE:
                    access_allowed_masks.append(access_mask)
                elif ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
                    access_denied_masks.append(access_mask)

        return access_allowed_masks, access_denied_masks

    def principal_has_full_control_of_object(self, object_path, principal_name):
        access_allowed_masks, access_denied_masks = self.get_aces_for_principal_on_object(
            object_path, principal_name
        )

        return self.FULL_CONTROL_MASK in access_allowed_masks and len(access_denied_masks) == 0

    def principal_has_no_permissions_on_object(self, object_path, principal_name):
        access_allowed_masks, _ = self.get_aces_for_principal_on_object(object_path, principal_name)

        return len(access_allowed_masks) == 0


@pytest.mark.xfail(
    not has_posix_target_user() or not has_posix_disjoint_user(),
    reason="Must be running inside of the sudo_environment testing container.",
)
@pytest.mark.usefixtures("posix_target_user", "posix_disjoint_user")
class TestTempDirPosixUser:
    """Tests of the TempDir when the resulting directory is to be owned by
    a different user than the current process.
    """

    def test_defaults(self, posix_target_user: PosixSessionUser) -> None:
        # Ensure that we can create the temporary directory.

        # GIVEN
        tmpdir = Path(tempfile.gettempdir())
        uid = pwd.getpwnam(posix_target_user.user).pw_uid  # type: ignore
        gid = grp.getgrnam(posix_target_user.group).gr_gid  # type: ignore

        # WHEN
        result = TempDir(user=posix_target_user)

        # THEN
        assert result.path.parent == tmpdir
        assert os.path.exists(result.path)
        statinfo = os.stat(result.path)
        assert statinfo.st_uid != uid, "Test: Not owned by target user"
        assert statinfo.st_uid == os.getuid(), "Test: Is owned by this user"  # type: ignore
        assert statinfo.st_gid == gid, "Test: gid is changed"
        assert statinfo.st_mode & stat.S_IWGRP, "Test: Directory is group-writable"

    def test_cleanup(self, posix_target_user: PosixSessionUser) -> None:
        # Ensure that we can delete the files in that directory that have been
        # created by the other user.

        # GIVEN
        tmpdir = TempDir(user=posix_target_user)
        testfilename = tmpdir.path / "testfile.txt"
        # Create a file owned by the target user and their default group.
        runresult = run(
            ["sudo", "-u", posix_target_user.user, "-i", "/usr/bin/touch", str(testfilename)],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        )

        # WHEN
        tmpdir.cleanup()

        # THEN
        assert runresult.returncode == 0
        assert not os.path.exists(testfilename)
        assert not os.path.exists(tmpdir.path)

    def test_cannot_change_to_group(self, posix_disjoint_user: PosixSessionUser) -> None:
        # Test that we raise an exception when we try to give the created directory to
        # a group that this process isn't a member of.

        # WHEN
        with pytest.raises(RuntimeError):
            TempDir(user=posix_disjoint_user)
