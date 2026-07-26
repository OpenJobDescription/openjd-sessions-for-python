# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from tempfile import gettempdir, mkdtemp
import os
import shutil
import stat
from pathlib import Path
from subprocess import DEVNULL, run
from typing import Any, Callable

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
    nonexistent_group_name,
    resolvable_member_groups,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    POSIX_SET_DISJOINT_USER_ENV_VARS_MESSAGE,
)


def spy_mkdtemp(created: list[str]) -> Callable[..., str]:
    """A `mkdtemp` that records what it creates, for asserting on a directory whose
    path the failed constructor never handed back.

    Recording the exact path, rather than diffing a directory listing, is what
    makes the assertion safe where the session root is shared: another test
    creating its own session directory there cannot affect it.
    """

    def _mkdtemp(**kwargs: Any) -> str:
        path = mkdtemp(**kwargs)
        created.append(path)
        return path

    return _mkdtemp


@pytest.mark.skipif(not is_posix(), reason="Posix-specific tests")
class TestTempDirPosix:
    def test_unresolvable_group_raises_runtimeerror(self, tmp_path: Path) -> None:
        """Pins: a group name that does not resolve must fail TempDir() as
        RuntimeError, the error this constructor documents.

        shutil.chown raises LookupError for an unknown group, and LookupError is
        neither OSError nor ValueError, so it escaped the `except OSError` around
        the ownership change here (and every other handler in the session-setup
        chain) and reached the caller of the public Session API unchanged.
        PosixSessionUser does not validate its group, so a caller only has to
        pass a group that does not exist.

        pytest.raises(RuntimeError) does not catch LookupError, so if the
        translation is removed this test errors out with the escaping
        LookupError -- which is the defect itself.
        """
        # GIVEN
        # Only `group` matters on this path; the user is never resolved by it.
        user = PosixSessionUser(user="nobody", group=nonexistent_group_name())

        # WHEN
        with pytest.raises(RuntimeError) as excinfo:
            TempDir(dir=tmp_path, user=user)

        # THEN
        # The failure is the group ownership change, not something incidental:
        # the offending group name is carried through to the message.
        assert user.group in str(excinfo.value)

    def test_failed_ownership_change_leaves_no_directory(self, tmp_path: Path) -> None:
        """Pins: a construction that fails at the ownership step removes the
        directory it had already created.

        mkdtemp() runs before the ownership change, so a failure there used to
        leave the directory behind with no way to reach it: __init__ raised, so
        the caller never received a TempDir and has no path to call cleanup()
        with. In production that directory is not under a per-test tmp_path but
        under the shared `<tempdir>/OpenJD` root, which nothing else ever prunes,
        so each failed session leaks one more 0o700 directory there.

        Asserting on the directory listing, rather than on the exception alone, is
        what makes this a pin: the pre-fix code raises the same RuntimeError.
        """
        # GIVEN
        # An unresolvable group reaches the failure path without needing any
        # privileges to arrange: PosixSessionUser does not validate its group.
        user = PosixSessionUser(user="nobody", group=nonexistent_group_name())
        before = set(os.listdir(tmp_path))
        created: list[str] = []

        # WHEN
        # Matched, not bare: this constructor raises RuntimeError for several
        # earlier failures too (mkdtemp itself, for one), and those never create a
        # directory, so a bare match could pass without the leak path running.
        with patch("openjd.sessions._tempdir.mkdtemp", side_effect=spy_mkdtemp(created)):
            with pytest.raises(RuntimeError, match="Could not change ownership"):
                TempDir(dir=tmp_path, user=user)

        # THEN
        assert set(os.listdir(tmp_path)) == before
        # AND, named exactly rather than inferred from the listing: TempDir resolves
        # `dir` before creating anything, so a listing on its own could in principle
        # be looking at the wrong directory.
        assert created, "test setup: mkdtemp was not reached, so nothing could have leaked"
        assert not os.path.exists(created[0])

    def test_successful_ownership_change_keeps_the_directory(self, tmp_path: Path) -> None:
        """The counterpart: the cleanup-on-failure must not fire on success.

        A guard that removed the directory unconditionally would pass the leak
        test above and break every real session, so the success path is pinned in
        the same place -- including the widened mode, since that is set after the
        ownership change and inside the same guarded block.
        """
        # GIVEN a group this process is a member of whose gid the new directory
        # would NOT already have. chown to one's own group needs no privileges, so
        # this exercises the real ownership path on any POSIX host without a
        # provisioned test user -- but only a *different* gid proves the chown
        # happened, since mkdtemp already inherits the parent's gid.
        inherited_gid = os.stat(tmp_path).st_gid
        candidates = [
            (gid, name) for gid, name in resolvable_member_groups() if gid != inherited_gid
        ]
        if not candidates:
            pytest.skip("this process is a member of no group other than the one it would inherit")
        gid, group_name = candidates[0]
        user = PosixSessionUser(user=pwd.getpwuid(os.geteuid()).pw_name, group=group_name)  # type: ignore

        # WHEN
        result = TempDir(dir=tmp_path, user=user)

        # THEN
        assert result.path.is_dir()
        statinfo = os.stat(result.path)
        assert statinfo.st_gid == gid
        # 0o770: mkdtemp's 0o700 widened for the group, and no wider.
        assert stat.S_IMODE(statinfo.st_mode) == stat.S_IRWXU | stat.S_IRWXG

    @pytest.mark.skipif(
        os.geteuid() == 0 if is_posix() else True,  # type: ignore
        reason="root is not subject to directory permissions",
    )
    def test_cleanup_failure_does_not_replace_the_ownership_error(self, tmp_path: Path) -> None:
        """Pins: the cleanup is best-effort, and the failure it cleans up after is
        still the one the caller sees.

        An exception raised inside an `except` block replaces the one being
        handled. If the removal were not best-effort, a session whose group could
        not be set would report a permission error about a temporary directory
        instead of naming the group -- turning a diagnosable configuration
        mistake into a confusing one, and only on the hosts where cleanup
        happens to fail.

        The removal is made to fail for real, by taking write permission off the
        parent directory at the moment the ownership change fails: rmdir() needs
        write access to the parent, not to the directory being removed.

        Skipped as root, which ignores directory permissions -- so a green run as
        root is not evidence for this behaviour. It is covered by the
        `localuser_sudo_environment` container, which runs the suite as `hostuser`.
        """
        # GIVEN
        user = PosixSessionUser(user="nobody", group=nonexistent_group_name())
        before = set(os.listdir(tmp_path))
        original_mode = stat.S_IMODE(os.stat(tmp_path).st_mode)

        def fail_and_make_the_parent_unwritable(path: Path, group: str) -> None:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
            raise OSError(f"no such group: {group}")

        try:
            with patch(
                "openjd.sessions._tempdir.chown_group",
                side_effect=fail_and_make_the_parent_unwritable,
            ):
                # WHEN
                with pytest.raises(RuntimeError) as excinfo:
                    TempDir(dir=tmp_path, user=user)

            # THEN the ownership failure survived the failed cleanup.
            assert user.group in str(excinfo.value)
            # AND the cleanup really did fail, so this test is exercising the
            # best-effort path rather than the ordinary one.
            assert set(os.listdir(tmp_path)) != before
        finally:
            # Restored to what it was, so that this test's temporary directory can
            # be collected however pytest chose to create it.
            os.chmod(tmp_path, original_mode)

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
        created: list[str] = []

        # THEN
        with patch("openjd.sessions._tempdir.mkdtemp", side_effect=spy_mkdtemp(created)):
            with pytest.raises(RuntimeError, match="Could not change permissions of directory"):
                TempDir(user=windows_user)

        # AND the directory that had already been created was removed. This is the
        # Windows half of the same leak as
        # TestTempDirPosix::test_failed_ownership_change_leaves_no_directory, and it
        # matters more here: no `dir` was passed, so the directory was created in
        # the shared `%PROGRAMDATA%\Amazon\OpenJD` root that nothing else prunes.
        assert created, "test setup: mkdtemp was not reached, so nothing could have leaked"
        assert not os.path.exists(created[0])

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
