# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Resolution of system command names to absolute paths, without consulting PATH.

The problem: a session launches its own privileged helpers (``sudo``, ``setsid``,
``kill``) with the environment it also gives the job, and that environment
includes the job's ``PATH``. A bare command name in such an argv is resolved
through that ``PATH``, so a job that puts an executable named ``sudo`` early on it
has that executable run at the session's privilege level rather than the job
user's.

The solution: never let a command name reach ``execvp``-style resolution. Callers
pass a bare name here and get back an absolute path found by scanning a fixed
list of trusted directories.

Three properties make that work, and all three are easy to undo by accident:

* ``PATH`` is never read. Not directly, and not through :func:`shutil.which` or
  ``command -v``, which resolve via ``PATH`` and so would restore the original
  behaviour while looking like a fix.
* Only paths under :data:`TRUSTED_SYSTEM_DIRECTORIES` are returned. A name
  containing a path separator is rejected, because ``os.path.join`` would
  otherwise let ``../../tmp/evil`` escape the directory being searched.
* A command that cannot be found raises. Returning the bare name as a fallback
  would put resolution back on ``PATH`` while the code still read as though it
  did not.

This module is POSIX-oriented, and :data:`TRUSTED_SYSTEM_DIRECTORIES` lists POSIX
locations. On Windows nothing is found and the lookups raise, which suits this
library because the cross-user paths needing these commands are POSIX-only.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional, Tuple

__all__ = [
    "SystemCommandNotFoundError",
    "TRUSTED_SYSTEM_DIRECTORIES",
    "find_system_command",
    "system_command_path",
]


TRUSTED_SYSTEM_DIRECTORIES: Tuple[str, ...] = (
    # Ordered, and the order is deliberate. On NixOS the setuid `sudo` wrapper
    # lives in /run/wrappers/bin and the /usr/bin copy is either absent or not
    # setuid, so the wrapper directory has to be consulted first. Everywhere else
    # this directory does not exist and costs one stat().
    "/run/wrappers/bin",
    # ...and the two NixOS entries are a pair. /run/wrappers/bin holds *only* the
    # setuid/setcap wrappers, so on NixOS it resolves `sudo` and nothing else:
    # /usr/bin holds just `env`, /bin just `sh`, and the sbin directories do not
    # exist. `setsid` and `pgrep` live in this symlink farm, which nixos-rebuild
    # manages and root owns, making it trust-equivalent to /usr/bin there.
    #
    # Without this entry the ordering above buys nothing: a cross-user session
    # would resolve `sudo` and then fail on `setsid` one line later. The pairing
    # is asserted in TestTrustedDirectories so it cannot be half-removed.
    "/run/current-system/sw/bin",
    "/usr/bin",
    "/bin",
    # sbin entries are last: on non-usr-merged distributions (some Debian
    # releases) some system commands exist only under /sbin.
    "/usr/sbin",
    "/sbin",
)


class SystemCommandNotFoundError(FileNotFoundError):
    """A required system command was not present in any trusted directory.

    A :class:`FileNotFoundError`, and therefore an :class:`OSError`, on purpose.

    An earlier revision of this module deliberately made it a plain ``Exception``,
    reasoning that "a privileged helper is unavailable" must not be absorbed by
    handlers that catch ``OSError`` to mean "carry on degraded". That reasoning was
    wrong here, because it assumed rather than checked what those handlers do.
    ``_runner_base``'s cancel path catches ``OSError`` around
    ``notify()``/``terminate()`` precisely so a failure to signal does not unwind an
    in-progress cancelation -- its own comment says "a cancel path is the wrong
    place to raise". Escaping that handler would lose the cancel's bookkeeping,
    which is worse than the warning it logs.

    So the semantics this class wants are exactly ``FileNotFoundError``'s: the thing
    we tried to launch is not there. Remaining a distinct type still lets a caller
    that cares tell "not in any trusted directory" apart from "``exec`` failed", and
    the message says which.
    """


def _validate_command_name(name: str) -> None:
    """Reject anything that is not a bare command name.

    Without this the resolver would itself become the injection point it exists
    to remove: ``os.path.join("/usr/bin", "../../tmp/evil")`` escapes the trusted
    directory entirely, so a caller that passed attacker-influenced text would be
    no better off than before.
    """
    if not name:
        raise ValueError("A system command name must not be empty.")
    if name in (os.curdir, os.pardir):
        raise ValueError(f"{name!r} is not a system command name.")
    # Checking both separators regardless of platform. A backslash is a legal
    # filename character on POSIX, but no command this module resolves contains
    # one, and treating it as suspect keeps the check identical on both
    # platforms rather than subtly weaker on one.
    #
    # The colon is rejected for the same reason, and it is not hypothetical:
    # ntpath.join(r"C:\Windows\System32", "D:evil") == "D:evil". A drive-relative
    # name discards the trusted prefix entirely while containing no separator at
    # all, so a separator-only check lets it through. POSIX joins it harmlessly,
    # but the guard belongs here rather than depending on which os.path is loaded.
    if "/" in name or "\\" in name or ":" in name:
        raise ValueError(
            f"A system command name must not contain a path separator or drive "
            f"specifier, but got {name!r}."
        )


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


@lru_cache(maxsize=None)
def find_system_command(name: str) -> Optional[str]:
    """Return the absolute path to ``name``, or ``None`` if it is not installed.

    Searches :data:`TRUSTED_SYSTEM_DIRECTORIES` in order. ``PATH`` is not
    consulted. Use this for commands whose absence is tolerable; use
    :func:`system_command_path` when the command is required.

    The result is cached: the filesystem layout does not change underneath a
    running session, and these lookups sit on process-launch and
    signal-delivery paths. Tests that patch
    :data:`TRUSTED_SYSTEM_DIRECTORIES` must call
    ``find_system_command.cache_clear()``.

    Raises:
        ValueError: if ``name`` is not a bare command name.
    """
    _validate_command_name(name)
    for directory in TRUSTED_SYSTEM_DIRECTORIES:
        candidate = os.path.join(directory, name)
        if _is_executable_file(candidate):
            return candidate
    return None


def system_command_path(name: str) -> str:
    """Return the absolute path to ``name``.

    Raises:
        ValueError: if ``name`` is not a bare command name.
        SystemCommandNotFoundError: if ``name`` is not in any trusted directory.
    """
    path = find_system_command(name)
    if path is None:
        raise SystemCommandNotFoundError(
            f"Could not find the system command {name!r} in any trusted directory "
            f"({', '.join(TRUSTED_SYSTEM_DIRECTORIES)}). PATH is deliberately not "
            f"searched; see openjd.sessions._system_commands."
        )
    return path
