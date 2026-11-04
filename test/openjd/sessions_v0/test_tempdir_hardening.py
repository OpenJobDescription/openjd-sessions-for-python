# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Hardening of the shared session temporary root and its cleanup.

``<tempdir>/OpenJD`` is a fixed, predictable path whose parent is world-writable
on typical POSIX hosts, and it is shared with the job user.
"""

import os
import shutil
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from openjd.sessions._logging import LogContent
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._tempdir import (
    _CLEANUP_RETRY_DELAYS_SECONDS,
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

    @pytest.mark.skipif(
        not is_posix(), reason="forces the POSIX branch, whose O_NOFOLLOW/O_DIRECTORY are absent"
    )
    def test_a_posix_platform_still_uses_a_descriptor(self, tmp_path: Path) -> None:
        """Negative control for the test above: POSIX must keep the fd path.

        The fd path is what makes the symlink-swap window unexploitable, so a
        mutation that routed *everything* to the weaker `lstat` validator has to
        fail something.

        POSIX-only, and not merely by preference: it drives the POSIX branch
        directly, and that branch names `os.O_NOFOLLOW`/`os.O_DIRECTORY`, which
        do not exist on every Windows build. The mutation this controls for is
        only ever evaluated on a POSIX host anyway.
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
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", ()),
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


def _rmtree_failing_first(fail_count: int) -> Any:
    """A stand-in for `shutil.rmtree` whose first `fail_count` calls report an error
    for the root without deleting anything, then behave like the real one."""
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def fake(path: Any, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= fail_count:
            error = PermissionError(32, "The process cannot access the file", str(path))
            if "onexc" in kwargs:
                kwargs["onexc"](os.unlink, path, error)
            else:
                kwargs["onerror"](os.unlink, path, (type(error), error, None))
            return
        real_rmtree(path, *args, **kwargs)

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


class TestCleanupRetry:
    """A process that has just exited can still hold handles in the session directory,
    and Windows refuses to delete a file while a handle to it is open."""

    def test_windows_retries_until_the_directory_is_deleted(self, tmp_path: Path) -> None:
        # GIVEN: a deletion that fails twice, then succeeds
        d = TempDir(dir=tmp_path)
        (d.path / "a.txt").write_text("x")
        fake = _rmtree_failing_first(2)

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=fake),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0, 0.0)),
        ):
            d.cleanup()

        # THEN
        assert fake.calls["n"] == 3
        assert not d.path.exists()

    def test_windows_raises_once_the_retries_are_exhausted(self, tmp_path: Path) -> None:
        # GIVEN: a deletion that never succeeds
        d = TempDir(dir=tmp_path)
        fake = _rmtree_failing_first(99)

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=fake),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0, 0.0)),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                d.cleanup()

        # THEN: one attempt plus one per delay, and the last failure is reported
        assert fake.calls["n"] == 3
        assert "could not be deleted" in str(excinfo.value)
        assert "PermissionError" in str(excinfo.value)

    def test_windows_waits_the_configured_delays(self, tmp_path: Path) -> None:
        # GIVEN
        d = TempDir(dir=tmp_path)
        fake = _rmtree_failing_first(99)

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=fake),
            patch("openjd.sessions._tempdir.time.sleep") as sleep,
        ):
            with pytest.raises(RuntimeError):
                d.cleanup()

        # THEN
        assert [c.args[0] for c in sleep.call_args_list] == list(_CLEANUP_RETRY_DELAYS_SECONDS)

    def test_posix_does_not_retry(self, tmp_path: Path) -> None:
        # GIVEN
        d = TempDir(dir=tmp_path)
        fake = _rmtree_failing_first(99)

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=False),
            patch("openjd.sessions._tempdir.rmtree", side_effect=fake),
            patch("openjd.sessions._tempdir.time.sleep") as sleep,
        ):
            with pytest.raises(RuntimeError):
                d.cleanup()

        # THEN
        assert fake.calls["n"] == 1
        sleep.assert_not_called()

    def test_missing_directory_is_still_an_error(self, tmp_path: Path) -> None:
        # GIVEN: a directory removed by someone else before cleanup
        d = TempDir(dir=tmp_path)
        os.rmdir(d.path)

        # WHEN / THEN: unchanged by the retry, on either platform
        for windows in (True, False):
            d2 = TempDir.__new__(TempDir)
            d2.path = d.path
            with (
                patch("openjd.sessions._tempdir.is_windows", return_value=windows),
                patch("openjd.sessions._tempdir.time.sleep") as sleep,
            ):
                with pytest.raises(RuntimeError):
                    d2.cleanup()
            sleep.assert_not_called()

    @pytest.mark.skipif(not is_windows(), reason="Windows mandatory file locking")
    def test_windows_file_held_open_briefly_is_deleted(self, tmp_path: Path) -> None:
        # GIVEN: a file in the session directory held open by another thread for
        # less time than the retries cover. Python's open() does not grant
        # FILE_SHARE_DELETE, so deleting it fails with a sharing violation.
        d = TempDir(dir=tmp_path)
        held = open(d.path / "locked.bin", "wb")
        release = threading.Timer(0.5, held.close)
        release.start()

        # WHEN
        try:
            d.cleanup()
        finally:
            release.cancel()
            held.close()

        # THEN
        assert not d.path.exists()

    @pytest.mark.skipif(not is_windows(), reason="Windows mandatory file locking")
    def test_windows_file_held_open_throughout_is_reported(self, tmp_path: Path) -> None:
        # GIVEN
        d = TempDir(dir=tmp_path)
        held = open(d.path / "locked.bin", "wb")

        # WHEN
        try:
            with patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.1, 0.1)):
                with pytest.raises(RuntimeError) as excinfo:
                    d.cleanup()
        finally:
            held.close()

        # THEN
        assert "locked.bin" in str(excinfo.value)

    @pytest.mark.skipif(not is_windows(), reason="Windows mandatory file locking")
    def test_windows_failed_attempt_names_this_process_as_the_holder(self, tmp_path: Path) -> None:
        # GIVEN: a file this process holds open for the whole cleanup
        d = TempDir(dir=tmp_path)
        held = open(d.path / "locked.bin", "wb")
        logger = MagicMock()

        # WHEN
        try:
            with patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.1,)):
                with pytest.raises(RuntimeError):
                    d.cleanup(logger=logger)
        finally:
            held.close()

        # THEN: one warning per failed attempt, each naming the file and this process
        messages = [c.args[0] for c in logger.warning.call_args_list]
        assert len(messages) == 2
        for message in messages:
            assert "locked.bin" in message
            assert "locked open by: " in message
            assert f"(pid {os.getpid()})" in message
        assert messages[0].endswith("Retrying in 0.1 s.")
        assert messages[1].endswith("Giving up.")


def _rmtree_failing_on(path: Path, fail_count: int) -> Any:
    """Like `_rmtree_failing_first`, but reports `path` (a real file) as undeletable."""
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def fake(root: Any, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= fail_count:
            error = PermissionError(32, "The process cannot access the file", str(path))
            if "onexc" in kwargs:
                kwargs["onexc"](os.unlink, str(path), error)
            else:
                kwargs["onerror"](os.unlink, str(path), (type(error), error, None))
            return
        real_rmtree(root, *args, **kwargs)

    return fake


class TestCleanupLockLogging:
    """Each failed Windows attempt is logged so an operator can see which files are
    still locked and which process holds them."""

    def test_retry_delays_double_from_one_second(self) -> None:
        assert _CLEANUP_RETRY_DELAYS_SECONDS == (1.0, 2.0, 4.0, 8.0, 16.0)

    def test_each_failed_attempt_is_logged_with_files_holders_and_next_delay(
        self, tmp_path: Path
    ) -> None:
        # GIVEN: a file that cannot be deleted for two passes, held by two processes
        d = TempDir(dir=tmp_path)
        locked = d.path / "sub" / "locked.bin"
        locked.parent.mkdir()
        locked.write_text("x")
        holders = [(42, "UnrealShaderCompileWorker.exe"), (42, "dup"), (7, "p4.exe")]
        logger = MagicMock()

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=_rmtree_failing_on(locked, 2)),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0, 4.0, 8.0)),
            patch("openjd.sessions._tempdir.time.sleep"),
            patch(
                "openjd.sessions._tempdir._find_lock_holders", return_value=(holders, None)
            ) as find,
        ):
            d.cleanup(logger=logger)

        # THEN: two warnings, then success; the lookup was given the locked file
        assert not d.path.exists()
        find.assert_called_with([str(locked)])
        messages = [c.args[0] for c in logger.warning.call_args_list]
        sub_locked = str(Path("sub") / "locked.bin")
        assert messages == [
            f"Could not delete temporary directory {d.path}: The process cannot access the file."
            f" 1 file(s) could not be deleted, including {sub_locked}. Files are still locked"
            f" open by: UnrealShaderCompileWorker.exe (pid 42), p4.exe (pid 7). Retrying in {r} s."
            for r in ("0", "4")
        ]
        extra = logger.warning.call_args.kwargs["extra"]
        assert LogContent.PROCESS_CONTROL in extra["openjd_log_content"]

    def test_last_failed_attempt_says_giving_up_and_reports_lookup_errors(
        self, tmp_path: Path
    ) -> None:
        # GIVEN: a file that can never be deleted, and a lookup that fails
        d = TempDir(dir=tmp_path)
        locked = d.path / "locked.bin"
        locked.write_text("x")
        logger = MagicMock()

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=_rmtree_failing_on(locked, 99)),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0,)),
            patch(
                "openjd.sessions._tempdir._find_lock_holders",
                return_value=([], "RmGetList failed (5)"),
            ),
        ):
            with pytest.raises(RuntimeError):
                d.cleanup(logger=logger)

        # THEN
        messages = [c.args[0] for c in logger.warning.call_args_list]
        assert len(messages) == 2
        assert "could not be determined: RmGetList failed (5)." in messages[1]
        assert messages[1].endswith("Giving up.")

    def test_no_holder_lookup_when_only_directories_failed(self, tmp_path: Path) -> None:
        # GIVEN: a deletion failing on the directory itself, not a file
        d = TempDir(dir=tmp_path)
        logger = MagicMock()

        # WHEN
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=_rmtree_failing_first(1)),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0,)),
            patch("openjd.sessions._tempdir._find_lock_holders") as find,
        ):
            d.cleanup(logger=logger)

        # THEN
        find.assert_not_called()
        (message,) = [c.args[0] for c in logger.warning.call_args_list]
        assert "locked" not in message
        assert message.endswith("Retrying in 0 s.")

    def test_posix_logs_nothing_extra(self, tmp_path: Path) -> None:
        d = TempDir(dir=tmp_path)
        logger = MagicMock()
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=False),
            patch("openjd.sessions._tempdir.rmtree", side_effect=_rmtree_failing_first(99)),
        ):
            with pytest.raises(RuntimeError):
                d.cleanup(logger=logger)
        logger.warning.assert_not_called()

    def test_defaults_to_the_module_logger(self, tmp_path: Path) -> None:
        d = TempDir(dir=tmp_path)
        with (
            patch("openjd.sessions._tempdir.is_windows", return_value=True),
            patch("openjd.sessions._tempdir.rmtree", side_effect=_rmtree_failing_first(1)),
            patch("openjd.sessions._tempdir._CLEANUP_RETRY_DELAYS_SECONDS", (0.0,)),
            patch("openjd.sessions._tempdir.LOG") as log,
        ):
            d.cleanup()
        log.warning.assert_called_once()


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
            """Probe the lowest free descriptor number by taking and releasing one.

            A `with` block is not usable here: the number itself is the
            measurement, so the descriptor has to be closed before it is returned.
            `try/finally` guarantees that on every path.
            """
            fd = os.open(os.devnull, os.O_RDONLY)
            try:
                return fd
            finally:
                os.close(fd)

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
