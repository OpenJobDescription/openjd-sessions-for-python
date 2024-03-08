# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from tempfile import gettempdir
import os
import shutil
import stat
from pathlib import Path
from subprocess import DEVNULL, run

from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._windows_permission_helper import WindowsPermissionHelper
from utils.windows_acl_helper import (
    MODIFY_READ_WRITE_MASK,
    FULL_CONTROL_MASK,
    get_aces_for_object,
    principal_has_access_to_object,
)

if is_posix():
    import grp
    import pwd

if is_windows():
    from openjd.sessions._win32._helpers import get_process_user  # type: ignore

import pytest
from unittest.mock import patch

from openjd.sessions import PosixSessionUser, WindowsSessionUser
from openjd.sessions._tempdir import TempDir, custom_gettempdir

from .conftest import (
    has_posix_disjoint_user,
    has_posix_target_user,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    POSIX_SET_DISJOINT_USER_ENV_VARS_MESSAGE,
)


@pytest.mark.skipif(not is_posix(), reason="Posix-specific tests")
class TestTempDirPosix:
    def test_defaults(self) -> None:
        # GIVEN
        tmpdir = Path(os.path.join(gettempdir(), "OpenJD")).resolve()

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
        tmpdir = Path(custom_gettempdir())
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
        dir = Path(gettempdir()) / "a" / "very" / "unlikely" / "dir" / "to" / "exist"

        # WHEN
        with pytest.raises(RuntimeError):
            TempDir(dir=dir)


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
class TestTempDirWindows:

    @pytest.mark.xfail(not has_windows_user(), reason=WIN_SET_TEST_ENV_VARS_MESSAGE)
    @pytest.mark.usefixtures("windows_user")
    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_object_permissions(self, mock_user_match, windows_user: WindowsSessionUser):
        # Test that TempDir gives the given WindowsSessionUser Modify/R/W, but not Full Control
        # permissions on the created directory.

        # GIVEN
        process_owner = get_process_user()
        if "\\" in process_owner:
            # Extract user from NETBIOS name
            process_owner = process_owner.split("\\")[1]
        elif "@" in process_owner:
            # Extract user from domain UPN
            process_owner = process_owner.split("@")[0]

        # WHEN
        tempdir = TempDir(user=windows_user)
        aces = get_aces_for_object(str(tempdir.path))

        # THEN
        assert len(aces) == 2  # Only self & user
        assert aces[process_owner][0] == [FULL_CONTROL_MASK]  # allowed
        assert aces[process_owner][1] == []  # denied
        assert aces[windows_user.user][0] == [MODIFY_READ_WRITE_MASK]  # allowed
        assert aces[windows_user.user][1] == []  # denied

    @pytest.mark.xfail(not has_windows_user(), reason=WIN_SET_TEST_ENV_VARS_MESSAGE)
    @pytest.mark.usefixtures("windows_user")
    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_windows_permissions_inherited(self, mock_user_match, windows_user: WindowsSessionUser):
        # WHEN
        tempdir = TempDir(user=windows_user)
        os.mkdir(tempdir.path / "child_dir")
        os.mkdir(tempdir.path / "child_dir" / "grandchild_dir")
        open(tempdir.path / "child_file", "a").close()
        open(tempdir.path / "child_dir" / "grandchild_file", "a").close()

        # THEN
        assert principal_has_access_to_object(
            str(tempdir.path), windows_user.user, MODIFY_READ_WRITE_MASK
        )
        assert principal_has_access_to_object(
            str(tempdir.path / "child_dir"), windows_user.user, MODIFY_READ_WRITE_MASK
        )
        assert principal_has_access_to_object(
            str(tempdir.path / "child_file"), windows_user.user, MODIFY_READ_WRITE_MASK
        )
        assert principal_has_access_to_object(
            str(tempdir.path / "child_dir" / "grandchild_dir"),
            windows_user.user,
            MODIFY_READ_WRITE_MASK,
        )
        assert principal_has_access_to_object(
            str(tempdir.path / "child_dir" / "grandchild_file"),
            windows_user.user,
            MODIFY_READ_WRITE_MASK,
        )

    @patch("openjd.sessions.WindowsSessionUser.is_process_user", return_value=True)
    def test_nonvalid_windows_principal_raises_exception(self, mock_user_match):
        # GIVEN
        windows_user = WindowsSessionUser("non_existent_user")

        # THEN
        with pytest.raises(RuntimeError, match="Could not change permissions of directory"):
            TempDir(user=windows_user)

    @pytest.fixture
    def clean_up_directory(self):
        created_dirs = []
        yield created_dirs
        for dir_path in created_dirs:
            if os.path.exists(dir_path):
                shutil.rmtree(dir_path)

    def test_windows_temp_dir(self, monkeypatch, clean_up_directory):
        monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramDataForOpenJDTest")
        expected_dir = r"C:\ProgramDataForOpenJDTest\Amazon\OpenJD"
        clean_up_directory.append(expected_dir)
        assert custom_gettempdir() == expected_dir
        assert os.path.exists(
            Path(expected_dir).parent
        ), r"Directory C:\ProgramDataForOpenJDTest\Amazon should be created."

    def test_cleanup(self, windows_user: WindowsSessionUser) -> None:
        # Ensure that we can delete the files in that directory that have been
        # created by the other user.

        # GIVEN
        tmpdir = TempDir(user=windows_user)
        testfilename = str(tmpdir.path / "testfile.txt")

        # Create a file on which only windows_user has permissions
        with open(testfilename, "w") as f:
            f.write("File content")
        WindowsPermissionHelper.set_permissions(
            testfilename, principals_full_control=[windows_user.user]
        )

        # WHEN
        tmpdir.cleanup()

        # THEN
        assert not os.path.exists(testfilename)
        assert not os.path.exists(tmpdir.path)


@pytest.mark.usefixtures("posix_target_user", "posix_disjoint_user")
class TestTempDirPosixUser:
    """Tests of the TempDir when the resulting directory is to be owned by
    a different user than the current process.
    """

    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    def test_defaults(self, posix_target_user: PosixSessionUser) -> None:
        # Ensure that we can create the temporary directory.

        # GIVEN
        tmpdir = Path(gettempdir())
        uid = pwd.getpwnam(posix_target_user.user).pw_uid  # type: ignore
        gid = grp.getgrnam(posix_target_user.group).gr_gid  # type: ignore

        # WHEN
        result = TempDir(user=posix_target_user)

        # THEN
        assert result.path.parent == tmpdir / "OpenJD"
        assert os.path.exists(result.path)
        statinfo = os.stat(result.path)
        assert statinfo.st_uid != uid, "Test: Not owned by target user"
        assert statinfo.st_uid == os.getuid(), "Test: Is owned by this user"  # type: ignore
        assert statinfo.st_gid == gid, "Test: gid is changed"
        assert statinfo.st_mode & stat.S_IWGRP, "Test: Directory is group-writable"

    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
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

    @pytest.mark.xfail(
        not has_posix_disjoint_user(),
        reason=POSIX_SET_DISJOINT_USER_ENV_VARS_MESSAGE,
    )
    def test_cannot_change_to_group(self, posix_disjoint_user: PosixSessionUser) -> None:
        # Test that we raise an exception when we try to give the created directory to
        # a group that this process isn't a member of.

        # WHEN
        with pytest.raises(RuntimeError):
            TempDir(user=posix_disjoint_user)
