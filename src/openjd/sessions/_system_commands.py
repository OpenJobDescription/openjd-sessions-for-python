# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Resolution of system command names to absolute paths, without consulting PATH.

This module exists because of CWE-426 (Untrusted Search Path). A session runs
job-supplied actions, and the job controls the environment those actions run
with -- including ``PATH``. That same environment is handed to the ``Popen`` call
that launches our *own* privileged helpers (``sudo``, ``setsid``, ``kill``), so a
bare command name in that argv is resolved through a search path the job wrote.
A job that drops an executable named ``sudo`` early on ``PATH`` gets it run at
the session's privilege level.

The fix is to never let a command name reach ``execvp``-style resolution. Every
name is resolved here instead, by scanning a fixed list of trusted absolute
directories.

Three properties are load-bearing:

.. warning::
   These properties are **not yet pinned by tests.** The regression suite for
   this module is outstanding work -- see §5.5 of the PATH-injection analysis for
   the table of properties that need falsifiable coverage. Until those exist,
   nothing here fails if the trusted-directory scan is replaced with
   :func:`shutil.which`, so treat the guarantees below as intent rather than as
   verified behaviour.


* **``PATH`` is never read.** Not directly, and not indirectly via
  :func:`shutil.which` or ``command -v`` -- both of which resolve through
  ``PATH`` and so would reintroduce the vulnerability while appearing to fix it.
* **Only paths under :data:`TRUSTED_SYSTEM_DIRECTORIES` are returned.**
* **A command that cannot be found raises.** Falling back to the bare name would
  silently restore the vulnerability, which is the worst available failure mode
  for this class of fix: the code would look fixed and behave as if it were not.

This module is POSIX-oriented; :data:`TRUSTED_SYSTEM_DIRECTORIES` lists POSIX
locations. On Windows nothing will be found and the lookups raise, which is
correct for this library because the cross-user code paths that need these
commands are POSIX-only.
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
    "/usr/bin",
    "/bin",
    # sbin entries are last: `shutdown` lives here, and on non-usr-merged
    # distributions (some Debian releases) it is *only* at /sbin/shutdown.
    "/usr/sbin",
    "/sbin",
)


class SystemCommandNotFoundError(Exception):
    """A required system command was not present in any trusted directory.

    Deliberately not a subclass of :class:`FileNotFoundError`. Callers around the
    subprocess machinery already catch ``OSError`` subclasses to mean "the thing
    I tried to launch is missing, carry on degraded", and this condition must not
    be absorbed by that handling: it means a privileged helper is unavailable, so
    the operation cannot proceed safely.
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
    if "/" in name or "\\" in name:
        raise ValueError(
            f"A system command name must not contain a path separator, but got {name!r}."
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
