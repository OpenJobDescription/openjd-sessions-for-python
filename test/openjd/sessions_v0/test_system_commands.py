# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the trusted-path command resolver.

``_system_commands`` fails silently when it fails at all: a resolver that quietly
falls back to ``PATH`` returns a working path for every command that is installed,
so it behaves identically to a correct one on any normal host. Only a caller with
an attacker-controlled ``PATH`` sees the difference.

These tests exist so that the difference is observable here instead. Each one
fails if the behaviour it describes is removed, which is what makes the resolver's
guarantees checkable rather than asserted.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from openjd.sessions._os_checker import is_posix
from openjd.sessions._system_commands import (
    SystemCommandNotFoundError,
    clear_command_cache,
    TRUSTED_SYSTEM_DIRECTORIES,
    find_system_command,
    system_command_path,
)

_MODULE = "openjd.sessions._system_commands"


@pytest.fixture(autouse=True)
def clear_resolver_cache():
    """Successful lookups are cached, so a path resolved under one patched
    directory list would otherwise leak into the next test."""
    clear_command_cache()
    yield
    clear_command_cache()


@pytest.fixture
def executable_dir(tmp_path: Path) -> Path:
    """A directory containing an executable file named ``target-cmd``."""
    target = tmp_path / "target-cmd"
    target.write_text("#!/bin/sh\ntrue\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


class TestOnlySearchesTrustedDirectories:
    def test_resolves_a_command_in_a_searched_directory(self, executable_dir: Path) -> None:
        """The negative control. Without it, the "not found" assertions below would
        be indistinguishable from a resolver that never finds anything."""
        # GIVEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(executable_dir),)):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result == str(executable_dir / "target-cmd")

    def test_does_not_resolve_a_command_outside_searched_directories(
        self, executable_dir: Path
    ) -> None:
        # GIVEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", ("/usr/bin", "/bin")):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None

    def test_ignores_path_even_when_it_contains_a_matching_command(
        self, executable_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins "PATH is never read".

        This is the property a ``shutil.which`` implementation would silently
        violate, and the one a PATH fallback for missing commands would undo. It
        is the closest thing this suite has to a direct test of the reported
        vulnerability: the job-controlled PATH must not influence resolution.
        """
        # GIVEN
        monkeypatch.setenv("PATH", str(executable_dir))
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", ("/usr/bin", "/bin")):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None, "PATH was consulted"

    def test_returns_the_first_matching_directory(self, tmp_path: Path) -> None:
        """Order is load-bearing: on NixOS the setuid sudo wrapper must win over a
        non-setuid /usr/bin copy."""
        # GIVEN
        first, second = tmp_path / "first", tmp_path / "second"
        for directory in (first, second):
            directory.mkdir()
            target = directory / "target-cmd"
            target.write_text("#!/bin/sh\ntrue\n")
            target.chmod(target.stat().st_mode | stat.S_IXUSR)

        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(first), str(second))):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result == str(first / "target-cmd")

    @pytest.mark.skipif(
        not is_posix(),
        reason="On Windows os.access(X_OK) is true for any existing file, so "
        "'not executable' is not expressible there",
    )
    def test_ignores_a_non_executable_file(self, tmp_path: Path) -> None:
        # GIVEN
        (tmp_path / "target-cmd").write_text("not executable")

        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(tmp_path),)):
            # WHEN
            result = find_system_command("target-cmd")

        # THEN
        assert result is None


class TestRejectsNonBareNames:
    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("a/b", id="forward-slash"),
            pytest.param("a\\b", id="backslash"),
            pytest.param("", id="empty"),
            pytest.param(".", id="curdir"),
            pytest.param("..", id="pardir"),
            # ntpath.join(r"C:\Windows\System32", "D:evil") == "D:evil" -- a
            # drive-relative name discards the trusted prefix while containing no
            # separator, so a separator-only guard lets it through.
            pytest.param("D:evil", id="drive-relative"),
            pytest.param("a:b", id="colon"),
        ],
    )
    def test_rejects_name_with_a_path_component(self, name: str) -> None:
        with pytest.raises(ValueError):
            find_system_command(name)

    def test_rejects_traversal_even_though_the_target_is_reachable(self, tmp_path: Path) -> None:
        """The guard must be about the name, not about whether the join happens to
        land on a real file -- so prove the target IS reachable by that join before
        asserting the name is refused.

        Without this, a test whose searched directory has nothing above it would
        pass even with the guard deleted, pinning nothing. That was verified: the
        parametrized test above does not catch the mutation on its own.
        """
        # GIVEN
        target = tmp_path / "target-cmd"
        target.write_text("#!/bin/sh\ntrue\n")
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        nested = tmp_path / "nested"
        nested.mkdir()
        assert os.path.isfile(
            os.path.join(str(nested), "../target-cmd")
        ), "precondition: the traversal target is reachable by this join"

        # WHEN / THEN
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(nested),)):
            with pytest.raises(ValueError, match="path separator"):
                find_system_command("../target-cmd")

    def test_rejection_is_valueerror_not_notfound(self) -> None:
        """A bad name is a caller bug; a missing command is an environment problem.
        Conflating them would let a caller mistake one for the other."""
        with pytest.raises(ValueError):
            system_command_path("a/b")


class TestMissingCommandRaises:
    def test_find_returns_none(self) -> None:
        assert find_system_command("openjd-definitely-not-installed") is None

    def test_raises_rather_than_returning_the_bare_name(self) -> None:
        """The silent-fallback failure mode: returning "sudo" here would look fixed
        and behave exactly as the vulnerability did."""
        with pytest.raises(SystemCommandNotFoundError) as excinfo:
            system_command_path("openjd-definitely-not-installed")

        message = str(excinfo.value)
        assert "openjd-definitely-not-installed" in message
        assert "PATH is deliberately not searched" in message

    def test_is_an_oserror_so_the_cancel_path_can_absorb_it(self) -> None:
        """This assertion is the inverse of what an earlier revision asserted, and
        the reversal is the point.

        That revision made the error a plain ``Exception`` on the theory that an
        unavailable privileged helper must not be absorbed by "carry on degraded"
        handlers. The theory was never checked against the handlers themselves.
        ``_runner_base``'s cancel path catches ``OSError`` around
        ``notify()``/``terminate()`` deliberately, so that failing to signal does
        not unwind an in-progress cancelation -- its comment says "a cancel path is
        the wrong place to raise". A non-OSError escapes that guard and costs the
        cancel its bookkeeping.

        Signalling reaches ``system_command_path("sudo")``, so this is a live path,
        not a hypothetical one.
        """
        assert issubclass(SystemCommandNotFoundError, OSError)
        assert issubclass(SystemCommandNotFoundError, FileNotFoundError)

    def test_is_still_distinguishable_from_a_plain_exec_failure(self) -> None:
        """Being a FileNotFoundError must not cost a caller the ability to tell
        "no trusted directory has it" apart from "exec failed"."""
        assert SystemCommandNotFoundError is not FileNotFoundError
        with pytest.raises(SystemCommandNotFoundError):
            system_command_path("openjd-definitely-not-installed")


class TestTrustedDirectories:
    def test_all_entries_are_absolute(self) -> None:
        """A relative entry would resolve against the process working directory,
        which a session changes."""
        for directory in TRUSTED_SYSTEM_DIRECTORIES:
            assert os.path.isabs(directory), f"{directory} is not absolute"

    def test_searches_the_setuid_wrapper_directory_before_usr_bin(self) -> None:
        assert TRUSTED_SYSTEM_DIRECTORIES.index(
            "/run/wrappers/bin"
        ) < TRUSTED_SYSTEM_DIRECTORIES.index("/usr/bin")

    def test_searches_both_sbin_locations(self) -> None:
        """On non-usr-merged distributions some system commands exist only under
        /sbin."""
        assert "/usr/sbin" in TRUSTED_SYSTEM_DIRECTORIES
        assert "/sbin" in TRUSTED_SYSTEM_DIRECTORIES

    def test_the_setuid_wrapper_precedes_the_nixos_symlink_farm(self) -> None:
        """Order between the two NixOS entries decides which `sudo` wins.

        `/run/current-system/sw/bin` also contains a `sudo`, but a non-setuid one:
        nix store paths cannot carry the setuid bit, which is the whole reason the
        wrapper directory exists. `_is_executable_file` checks only that the
        candidate is a file with an execute bit, so it cannot tell the two apart --
        tuple order is the only thing that does.

        Getting this backwards would resolve a `sudo` that cannot elevate, so
        cross-user sessions would fail on a host where the correct binary was
        present all along. The wrapper-before-/usr/bin assertion below does not
        cover it, because sw/bin sits between them.
        """
        directories = TRUSTED_SYSTEM_DIRECTORIES

        assert directories.index("/run/wrappers/bin") < directories.index(
            "/run/current-system/sw/bin"
        )

    def test_the_two_nixos_entries_are_present_as_a_pair(self) -> None:
        """/run/wrappers/bin alone supports no complete code path.

        It holds only the setuid wrappers, so on NixOS it resolves `sudo` and
        nothing else -- /usr/bin has just `env`, /bin just `sh`, and the sbin
        directories are absent. `setsid` and `pgrep` are in the sw/bin symlink
        farm. Keeping the wrapper entry without that one would resolve `sudo` and
        then fail on `setsid` one line later, so the ordering comment would be
        describing support that does not exist.
        """
        assert "/run/wrappers/bin" in TRUSTED_SYSTEM_DIRECTORIES
        assert "/run/current-system/sw/bin" in TRUSTED_SYSTEM_DIRECTORIES


@pytest.mark.skipif(
    not is_posix(),
    reason="TRUSTED_SYSTEM_DIRECTORIES is a POSIX layout; on Windows none of these "
    "directories exist and every lookup is expected to raise",
)
class TestRealCommandsResolve:
    """Positive controls against the real filesystem.

    The tests above use temporary directories, so they would all pass on a host
    where the genuine trusted directories were empty or misspelled. These assert
    that the commands the library actually launches are found where it looks.
    """

    @pytest.mark.parametrize("name", ["sh", "ls"])
    def test_a_universally_present_command_resolves(self, name: str) -> None:
        resolved = system_command_path(name)

        assert os.path.isabs(resolved)
        assert os.path.dirname(resolved) in TRUSTED_SYSTEM_DIRECTORIES

    def test_sudo_resolves_on_this_host(self) -> None:
        """sudo is the command the reported vulnerability abused. If this fails,
        cross-user sessions cannot start on this host -- which is exactly the
        portability regression that hardcoded literals introduced."""
        if find_system_command("sudo") is None:
            pytest.skip("sudo is not installed on this host")

        assert os.path.dirname(system_command_path("sudo")) in TRUSTED_SYSTEM_DIRECTORIES


class TestCacheDoesNotRememberAbsence:
    """Successes are cached; failures are not.

    A package manager replacing a binary unlinks and relinks it, so a lookup
    landing in that window finds nothing. Caching that answer would make one
    unlucky moment permanent for the rest of a long-lived agent's life: every
    later cross-user launch would fail on a command that is sitting on disk.
    """

    def test_a_command_that_appears_later_is_found(self, tmp_path: Path) -> None:
        # GIVEN a lookup that misses, as it would mid-upgrade
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(tmp_path),)):
            assert find_system_command("target-cmd") is None

            # WHEN the binary appears
            target = tmp_path / "target-cmd"
            target.write_text("#!/bin/sh\ntrue\n")
            target.chmod(target.stat().st_mode | stat.S_IXUSR)

            # THEN the next lookup finds it rather than repeating the cached miss
            assert find_system_command("target-cmd") == str(target)

    def test_a_resolved_path_is_cached(self, executable_dir: Path) -> None:
        """The other half of the asymmetry. Without this, 'do not cache misses'
        could be satisfied by caching nothing at all, and the reason the cache
        exists (these lookups sit on signal-delivery paths) would be lost."""
        # GIVEN one successful lookup
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", (str(executable_dir),)):
            first = find_system_command("target-cmd")
        assert first == str(executable_dir / "target-cmd")

        # WHEN the directory list no longer contains it, and the file is gone
        (executable_dir / "target-cmd").unlink()

        # THEN the cached answer is still returned, without touching the filesystem
        with patch(f"{_MODULE}.TRUSTED_SYSTEM_DIRECTORIES", ()):
            assert find_system_command("target-cmd") == first
