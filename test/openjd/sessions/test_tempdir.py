# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
import tempfile
from pathlib import Path
from subprocess import DEVNULL, run

if os.name == "posix":
    import grp
    import pwd

import pytest

from openjd.sessions import PosixSessionUser
from openjd.sessions._tempdir import TempDir

from .conftest import has_posix_disjoint_user, has_posix_target_user


class TestTempDir:
    def test_defaults(self) -> None:
        # GIVEN
        tmpdir = Path(tempfile.gettempdir())

        # WHEN
        result = TempDir()

        # THEN
        assert result.path.parent == tmpdir
        assert os.path.exists(result.path)
        statinfo = os.stat(result.path)
        assert statinfo.st_uid == os.getuid()
        assert statinfo.st_gid == os.getgid()

        os.rmdir(result.path)

    @pytest.mark.usefixtures("tmp_path")  # Built-in fixture
    def test_given_dir(self, tmp_path: Path) -> None:
        # WHEN
        result = TempDir(dir=tmp_path)

        # THEN
        assert result.path.parent == tmp_path
        assert os.path.exists(result.path)

    def test_given_prefix(self) -> None:
        # GIVEN
        tmpdir = Path(tempfile.gettempdir())
        prefix = "testprefix"

        # WHEN
        result = TempDir(prefix=prefix)

        # THEN
        assert result.path.parent == tmpdir
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
        uid = pwd.getpwnam(posix_target_user.user).pw_uid
        gid = grp.getgrnam(posix_target_user.group).gr_gid

        # WHEN
        result = TempDir(user=posix_target_user)

        # THEN
        assert result.path.parent == tmpdir
        assert os.path.exists(result.path)
        statinfo = os.stat(result.path)
        assert statinfo.st_uid != uid, "Test: Not owned by target user"
        assert statinfo.st_uid == os.getuid(), "Test: Is owned by this user"
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
