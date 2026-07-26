# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Hardening of the shared session temporary root and its cleanup.

``<tempdir>/OpenJD`` is a fixed, predictable path whose parent is world-writable
on typical POSIX hosts, and it is shared with the job user.
"""

import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


from openjd.sessions._os_checker import is_posix
from openjd.sessions._tempdir import OPENJD_TEMPDIR_MODE, TempDir, custom_gettempdir


class TestSharedTempRootValidation:
    """R5-3: `<tempdir>/OpenJD` is a fixed, predictable path whose parent is
    world-writable on typical POSIX hosts. `exist_ok=True` accepted whatever was
    already there."""

    def test_created_with_an_explicit_mode_regardless_of_umask(self, tmp_path: Path) -> None:
        # GIVEN: a hostile umask that would otherwise strip group/other bits
        old_umask = os.umask(0o077)
        try:
            with patch("openjd.sessions._tempdir.gettempdir", return_value=str(tmp_path)):
                # WHEN
                created = custom_gettempdir()
        finally:
            os.umask(old_umask)

        # THEN: the root is traversable as intended, not umask-dependent.
        assert Path(created) == tmp_path / "OpenJD"
        if is_posix():
            assert stat.S_IMODE(os.stat(created).st_mode) == stat.S_IMODE(OPENJD_TEMPDIR_MODE)

    @pytest.mark.skipif(not is_posix(), reason="symlink pre-creation is a POSIX vector here")
    def test_rejects_a_symlink_at_the_root_path(self, tmp_path: Path) -> None:
        # GIVEN: an attacker has replaced the root with a symlink elsewhere
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").symlink_to(elsewhere, target_is_directory=True)

        # WHEN / THEN: we refuse rather than creating sessions inside it. The
        # refusal now comes from O_NOFOLLOW on the open itself (see REG-3).
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError, match="real directory"):
                custom_gettempdir()

    def test_rejects_a_root_that_is_not_a_directory(self, tmp_path: Path) -> None:
        # GIVEN: a plain file squatting on the root path
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").write_text("squat")

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError):
                custom_gettempdir()

    @staticmethod
    def _fstat_reporting_uid(uid: int) -> Any:
        """Wrap os.fstat so the root directory appears owned by `uid`.

        Patches fstat rather than lstat: the implementation validates through a
        descriptor now (see REG-3), so an lstat mock would not be consulted and
        the test would vacuously pass.
        """
        real_fstat = os.fstat

        class _Stat:
            def __init__(self, base: os.stat_result) -> None:
                self.st_mode = base.st_mode
                self.st_uid = uid

        def fake_fstat(fd: int, *a: Any, **k: Any) -> Any:
            return _Stat(real_fstat(fd, *a, **k))

        return fake_fstat

    @pytest.mark.skipif(not is_posix(), reason="uid ownership check is POSIX-only")
    def test_rejects_a_root_owned_by_another_user(self, tmp_path: Path) -> None:
        # GIVEN: the root exists but is owned by a different uid
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").mkdir()

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch(
                "openjd.sessions._tempdir.os.fstat",
                side_effect=self._fstat_reporting_uid(os.geteuid() + 12345),  # type: ignore
            ):
                with pytest.raises(RuntimeError, match="owned by uid"):
                    custom_gettempdir()

    @pytest.mark.skipif(not is_posix(), reason="uid ownership check is POSIX-only")
    def test_accepts_a_root_owned_by_root(self, tmp_path: Path) -> None:
        """A system-provisioned root must keep working."""
        # GIVEN
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / "OpenJD").mkdir()

        # WHEN / THEN: no exception
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch(
                "openjd.sessions._tempdir.os.fstat", side_effect=self._fstat_reporting_uid(0)
            ):
                assert custom_gettempdir() == str(parent / "OpenJD")


# ===========================================================================
# R5-7 -- cleanup must report WHY each path could not be deleted
# ===========================================================================


class TestCleanupErrorReporting:
    """R5-7: the old handler accepted the exception and discarded it, leaving a
    list of bare paths -- on the one code path where "permission denied" versus
    "a process still holds this open" changes what the operator must do."""

    def test_failure_message_names_the_path_and_the_reason(self, tmp_path: Path) -> None:
        # GIVEN: a temp dir whose removal fails with a specific, diagnosable error
        d = TempDir(dir=tmp_path)
        doomed = d.path / "stubborn.txt"
        doomed.write_text("x")

        def boom(path: Any, *a: Any, **k: Any) -> None:
            raise PermissionError(13, "Permission denied")

        # WHEN
        with (
            patch("openjd.sessions._tempdir.os.unlink", side_effect=boom),
            patch("openjd.sessions._tempdir.os.remove", side_effect=boom, create=True),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                d.cleanup()

        # THEN: both the path and the cause are in the message.
        message = str(excinfo.value)
        assert "stubborn.txt" in message
        assert "PermissionError" in message

    def test_successful_cleanup_still_removes_everything(self, tmp_path: Path) -> None:
        # GIVEN
        d = TempDir(dir=tmp_path)
        (d.path / "a.txt").write_text("x")
        (d.path / "sub").mkdir()
        (d.path / "sub" / "b.txt").write_text("y")

        # WHEN
        d.cleanup()

        # THEN
        assert not d.path.exists()


# ===========================================================================
# R5-4 / R5-5 -- nothing unquoted may reach /bin/sh
# ===========================================================================


@pytest.mark.skipif(not is_posix(), reason="symlink swap and fchmod semantics are POSIX here")
class TestTempRootCheckThenUse:
    """The first R5-3 implementation validated with `lstat(path)` and then called
    `stat(path)`/`chmod(path)`, both of which re-resolve the name and follow
    links. Swapping the entry for a symlink in between defeated the check and
    chmod'ed the link's target to 0o755."""

    def test_a_symlink_swapped_in_before_the_open_is_refused(self, tmp_path: Path) -> None:
        """Swap during the create window: O_NOFOLLOW must refuse the open."""
        # GIVEN: a victim directory at 0o700 and a parent where the root will live
        victim = tmp_path / "victim"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent"
        parent.mkdir()
        root = parent / "OpenJD"

        real_makedirs = os.makedirs

        def makedirs_then_swap(path: Any, *a: Any, **k: Any) -> None:
            real_makedirs(path, *a, **k)
            if str(path) == str(root):
                os.rmdir(root)
                os.symlink(victim, root, target_is_directory=True)

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.makedirs", side_effect=makedirs_then_swap):
                with pytest.raises(RuntimeError):
                    custom_gettempdir()

        # THEN
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_a_symlink_swapped_in_after_the_open_cannot_redirect_the_chmod(
        self, tmp_path: Path
    ) -> None:
        """Swap *after* the descriptor is open, which O_NOFOLLOW cannot help with.

        This is the case that pins fchmod-vs-chmod. An earlier version of this
        test swapped during the create window instead, where O_NOFOLLOW refuses
        the open before any chmod is attempted -- so it passed even with
        `os.chmod(path)` restored, and pinned nothing about the descriptor. The
        swap is injected from inside `os.fstat`, which the implementation calls
        between the open and the mode change.
        """
        # GIVEN: a victim at 0o700, and a real root whose mode needs correcting
        victim = tmp_path / "victim_after"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent_after"
        parent.mkdir()
        root = parent / "OpenJD"
        root.mkdir()
        os.chmod(root, 0o700)  # != OPENJD_TEMPDIR_MODE, so a mode change is due

        real_fstat = os.fstat
        swapped = {"done": False}

        def fstat_then_swap(fd: int, *a: Any, **k: Any) -> Any:
            result = real_fstat(fd, *a, **k)
            if not swapped["done"]:
                swapped["done"] = True
                os.rmdir(root)
                os.symlink(victim, root, target_is_directory=True)
            return result

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with patch("openjd.sessions._tempdir.os.fstat", side_effect=fstat_then_swap):
                custom_gettempdir()

        # THEN: the swap happened, and the victim was NOT widened. With
        # `os.chmod(temp_dir, ...)` restored this is 0o755.
        assert swapped["done"] is True
        assert os.path.islink(root)
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_the_validated_inode_is_the_one_modified(self, tmp_path: Path) -> None:
        """Even when the swap is not detected as an error, the mode must land on
        the inode that was validated, never on a later resolution of the name."""
        # GIVEN: an existing root at a wrong mode, plus a victim
        victim = tmp_path / "victim2"
        victim.mkdir()
        os.chmod(victim, 0o700)
        parent = tmp_path / "parent2"
        parent.mkdir()
        root = parent / "OpenJD"
        root.mkdir()
        os.chmod(root, 0o700)  # differs from OPENJD_TEMPDIR_MODE, so a chmod is due

        # WHEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            result = custom_gettempdir()

        # THEN: the real root got the mode; the victim was not touched.
        assert result == str(root)
        assert stat.S_IMODE(os.stat(root).st_mode) == stat.S_IMODE(OPENJD_TEMPDIR_MODE)
        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

    def test_a_symlinked_root_is_still_refused(self, tmp_path: Path) -> None:
        """O_NOFOLLOW must keep doing what the explicit S_ISLNK branch did."""
        # GIVEN
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        parent = tmp_path / "parent3"
        parent.mkdir()
        (parent / "OpenJD").symlink_to(elsewhere, target_is_directory=True)

        # WHEN / THEN
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            with pytest.raises(RuntimeError, match="real directory"):
                custom_gettempdir()

    def test_no_descriptor_is_leaked(self, tmp_path: Path) -> None:
        """The validation opens a descriptor; every path must close it.

        Probe: the lowest free descriptor number. If a call leaks a descriptor,
        the number the kernel hands out next goes up. Portable across macOS and
        Linux, unlike listing /dev/fd.
        """
        # GIVEN
        parent = tmp_path / "parent4"
        parent.mkdir()
        (parent / "OpenJD").mkdir()

        def lowest_free_fd() -> int:
            fd = os.open(os.devnull, os.O_RDONLY)
            os.close(fd)
            return fd

        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            custom_gettempdir()  # warm up, so first-call allocations do not count
            baseline = lowest_free_fd()

            # WHEN: many successful calls
            for _ in range(25):
                custom_gettempdir()
            after_success = lowest_free_fd()

            # WHEN: many calls that fail *after* the descriptor is opened
            with patch(
                "openjd.sessions._tempdir.os.fstat", side_effect=OSError(5, "induced failure")
            ):
                for _ in range(25):
                    with pytest.raises(OSError):
                        custom_gettempdir()
            after_failure = lowest_free_fd()

        # THEN: no drift on either path.
        assert after_success == baseline
        assert after_failure == baseline
