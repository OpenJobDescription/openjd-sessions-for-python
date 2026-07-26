# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Hardening of the shared session temporary root and its cleanup.

``<tempdir>/OpenJD`` is a fixed, predictable path whose parent is world-writable
on typical POSIX hosts, and it is shared with the job user.
"""

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._tempdir import (
    OPENJD_TEMPDIR_MODE,
    TempDir,
    _prepare_temp_dir_root,
    _prepare_temp_dir_root_windows,
    custom_gettempdir,
)


def temp_root_under(parent: Path) -> Path:
    """The root `custom_gettempdir()` will use when its parent is redirected to `parent`.

    The two platforms nest differently: POSIX uses `<tempdir>/OpenJD`, Windows
    uses `%PROGRAMDATA%\\Amazon\\OpenJD`.
    """
    return parent / "Amazon" / "OpenJD" if is_windows() else parent / "OpenJD"


@contextmanager
def redirected_temp_root(parent: Path) -> Iterator[Path]:
    """Redirect `custom_gettempdir()` beneath `parent` on either platform.

    Patching `gettempdir` alone is not enough: on Windows `custom_gettempdir()`
    never calls it, so a `gettempdir`-only patch silently leaves the test
    operating on the real `%PROGRAMDATA%\\Amazon\\OpenJD`.
    """
    if is_windows():
        with patch.dict(os.environ, {"PROGRAMDATA": str(parent)}):
            yield temp_root_under(parent)
    else:
        with patch("openjd.sessions._tempdir.gettempdir", return_value=str(parent)):
            yield temp_root_under(parent)


class TestSharedTempRootValidation:
    """R5-3: `<tempdir>/OpenJD` is a fixed, predictable path whose parent is
    world-writable on typical POSIX hosts. `exist_ok=True` accepted whatever was
    already there."""

    def test_created_with_an_explicit_mode_regardless_of_umask(self, tmp_path: Path) -> None:
        # GIVEN: a hostile umask that would otherwise strip group/other bits
        old_umask = os.umask(0o077)
        try:
            with redirected_temp_root(tmp_path) as expected_root:
                # WHEN
                created = custom_gettempdir()
        finally:
            os.umask(old_umask)

        # THEN: the root is traversable as intended, not umask-dependent.
        assert Path(created) == expected_root
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
        squatter = temp_root_under(parent)
        squatter.parent.mkdir(parents=True)
        squatter.write_text("squat")

        # WHEN / THEN: makedirs() rejects it first on both platforms, so this
        # asserts the refusal, not which layer produced it.
        with redirected_temp_root(parent):
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

    def test_windows_validator_accepts_a_real_directory(self, tmp_path: Path) -> None:
        """The Windows branch must not try to `os.open()` a directory.

        Windows returns EACCES for `os.open()` on a directory whatever the flags,
        so an earlier version of this validator failed *every* session creation
        on Windows. `os.lstat` is portable, so this runs on all platforms and
        pins the branch against a regression back to a descriptor.
        """
        # GIVEN a real directory / WHEN validated / THEN it is accepted
        root = tmp_path / "OpenJD"
        root.mkdir()
        _prepare_temp_dir_root_windows(str(root))

    def test_a_non_posix_platform_never_opens_a_descriptor(self, tmp_path: Path) -> None:
        """The dispatcher must not send Windows down the descriptor path.

        This is the bug itself: `os.open()` on a directory returns EACCES on
        Windows whatever the flags, so routing Windows through the POSIX
        validator failed *every* session creation there. Pinned by patching
        `is_posix` rather than by running on Windows, so a POSIX CI host catches
        the regression too -- without this, the only signal is a Windows job.
        """
        # GIVEN a real directory, and a platform that reports as non-POSIX
        root = tmp_path / "OpenJD"
        root.mkdir()

        # WHEN validated
        with patch("openjd.sessions._tempdir.is_posix", return_value=False):
            with patch("openjd.sessions._tempdir.os.open") as opener:
                _prepare_temp_dir_root(str(root))

        # THEN no descriptor was ever taken
        opener.assert_not_called()

    def test_a_posix_platform_still_uses_a_descriptor(self, tmp_path: Path) -> None:
        """Negative control for the test above: POSIX must keep the fd path.

        The fd path is what makes the symlink-swap window unexploitable, so a
        mutation that routed *everything* to the weaker `lstat` validator has to
        fail something.
        """
        # GIVEN a real directory on a platform that reports as POSIX
        root = tmp_path / "OpenJD"
        root.mkdir()
        real_open = os.open
        opened: list[str] = []

        def recording_open(path: Any, *a: Any, **k: Any) -> int:
            opened.append(str(path))
            return real_open(path, *a, **k)

        # WHEN validated
        with patch("openjd.sessions._tempdir.is_posix", return_value=True):
            with patch("openjd.sessions._tempdir.os.open", side_effect=recording_open):
                _prepare_temp_dir_root(str(root))

        # THEN the root was validated through a descriptor
        assert str(root) in opened

    def test_windows_validator_rejects_a_non_directory(self, tmp_path: Path) -> None:
        # GIVEN a plain file where the root should be
        squatter = tmp_path / "OpenJD"
        squatter.write_text("squat")

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="real directory"):
            _prepare_temp_dir_root_windows(str(squatter))

    @pytest.mark.skipif(not is_posix(), reason="symlink creation is POSIX-only here")
    def test_windows_validator_rejects_a_symlink(self, tmp_path: Path) -> None:
        """`lstat` must not traverse the link, or a directory symlink would pass.

        Exercised with a POSIX symlink because that is what this host can create;
        on Windows the same `S_ISDIR` test rejects a directory symlink and
        `st_reparse_tag` rejects a junction.
        """
        # GIVEN
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = tmp_path / "OpenJD"
        link.symlink_to(elsewhere, target_is_directory=True)

        # WHEN / THEN
        with pytest.raises(RuntimeError, match="real directory"):
            _prepare_temp_dir_root_windows(str(link))

    def test_windows_validator_reports_an_unreadable_root(self, tmp_path: Path) -> None:
        # GIVEN a root that cannot be inspected at all
        missing = tmp_path / "OpenJD"

        # WHEN / THEN: the failure names the path rather than escaping as OSError
        with pytest.raises(RuntimeError, match="could not be inspected"):
            _prepare_temp_dir_root_windows(str(missing))

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
