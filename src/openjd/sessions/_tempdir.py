# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
import sys
from ._logging import LoggerAdapter, LogContent, LogExtraInfo
from pathlib import Path
from shutil import chown, rmtree
from tempfile import gettempdir, mkdtemp
from typing import Any, Optional, cast

from ._session_user import PosixSessionUser, SessionUser, WindowsSessionUser
from ._windows_permission_helper import WindowsPermissionHelper
from ._os_checker import is_posix, is_windows

if is_windows():
    from ._win32._helpers import get_process_user  # type: ignore


OPENJD_TEMPDIR_MODE = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
"""Mode for the shared Open Job Description temporary root: 0o755.

World-traversable on purpose. If this directory lacked group/other search
permission then a job user -- who by design is a different OS user from this
process -- could not reach the session working directory beneath it. The session
directories themselves are created 0o700 by mkdtemp() and only widened to 0o770
for the specific group named by a SessionUser, so the permissive mode here grants
traversal, not read access to any session's contents.

Applied explicitly rather than left to the process umask so that the resulting
mode does not depend on how the embedding application happens to be configured.
"""


def _prepare_temp_dir_root(temp_dir: str) -> None:
    """Validate the shared temporary root and set its mode, operating on a single
    file descriptor so the two cannot disagree.

    Why this exists: the root is a *fixed, predictable* path
    (``<tempdir>/OpenJD``) and on POSIX its parent is typically world-writable
    ``/tmp``. ``os.makedirs(..., exist_ok=True)`` happily accepts an entry that
    some other local user created first, or a symlink pointing somewhere else
    entirely -- and every session working directory would then be created inside
    a directory that user controls. ``/tmp``'s sticky bit does not help: it
    restricts deletion within ``/tmp`` itself, not within an attacker-owned
    ``/tmp/OpenJD``.

    Why a descriptor rather than the path: an earlier version of this validated
    with ``os.lstat(path)`` and then called ``os.stat(path)``/``os.chmod(path)``.
    Both of those resolve the *name* again and follow symlinks, so replacing the
    entry with a symlink in between defeated the check entirely -- and the chmod
    then widened the link's target to 0o755. Opening once with ``O_NOFOLLOW`` and
    ``O_DIRECTORY`` and using ``fstat``/``fchmod`` means every decision and every
    modification applies to the same inode we validated, whatever happens to the
    name afterwards.

    ``O_NOFOLLOW`` makes the open itself fail on a symlink, which is why there is
    no separate ``S_ISLNK`` branch on POSIX. Neither flag exists on Windows, where
    the ``getattr(..., 0)`` fallbacks make this a plain open and the reparse-point
    check below carries the equivalent weight.

    Raises:
        RuntimeError: if the path is a symlink or reparse point, is not a
            directory, is owned by another user, or its mode cannot be set.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(temp_dir, flags)
    except OSError as err:
        # ELOOP here is the O_NOFOLLOW refusal; ENOTDIR is O_DIRECTORY's.
        raise RuntimeError(
            f"Refusing to use temporary directory {temp_dir}: it could not be opened as a real "
            f"directory ({err}). If it is a symbolic link or not a directory, remove it, or pass "
            "an explicit session root directory."
        )
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_reparse_tag", 0) != 0:
            raise RuntimeError(
                f"Refusing to use temporary directory {temp_dir}: it is not a real directory."
            )
        if is_posix():
            this_uid = os.geteuid()
            # root is accepted so that a system-provisioned root directory works.
            if st.st_uid not in (this_uid, 0):
                raise RuntimeError(
                    f"Refusing to use temporary directory {temp_dir}: it is owned by uid "
                    f"{st.st_uid}, not by this process' uid ({this_uid}) or root. Another user "
                    "may have created it. Remove it, or pass an explicit session root directory."
                )

        # makedirs()' `mode` is masked by the process umask, so it does not on its
        # own guarantee anything: under umask 0o077 an "explicit" 0o755 lands as
        # 0o700, and a job user could then not traverse into its own session
        # directory. Set the mode outright. Conditional because a
        # system-provisioned root may be owned by root with the right mode
        # already, where the call would fail for no reason.
        if stat.S_IMODE(st.st_mode) != stat.S_IMODE(OPENJD_TEMPDIR_MODE):
            try:
                # fchmod, not chmod: applies to the validated inode, not to
                # whatever the name resolves to now.
                os.fchmod(fd, OPENJD_TEMPDIR_MODE)
            except OSError as err:
                raise RuntimeError(
                    f"Could not set permissions on temporary directory {temp_dir}: {err}. "
                    "Session working directories created beneath it may not be reachable by a "
                    "job user."
                )
    finally:
        os.close(fd)


def custom_gettempdir(logger: Optional[LoggerAdapter] = None) -> str:
    """
    Get a platform-specific temporary directory.

    For Windows systems, this function returns a specific directory path,
    '%PROGRAMDATA%\\Amazon\\'. If this directory does not exist, it will be created.
    For non-Windows systems, it returns the system's default temporary directory.

    Args:
        logger (Optional[LoggerAdapter]): The logger to which all messages should be sent from this and the
                subprocess.

    Returns:
        str: The path to the temporary directory specific to the operating system.

    Raises:
        RuntimeError: If the directory could not be created, or if it already
            exists and is not a directory owned by this process' user (see
            :func:`_prepare_temp_dir_root`).
    """
    if is_windows():
        program_data_path = os.getenv("PROGRAMDATA")
        if program_data_path is None:
            program_data_path = r"C:\ProgramData"
            if logger:
                logger.warning(
                    f'Environment variable "PROGRAMDATA" is not set. Creating the session working directories under {program_data_path}',
                    extra=LogExtraInfo(openjd_log_content=LogContent.FILE_PATH),
                )

        temp_dir_parent = os.path.join(program_data_path, "Amazon")
    else:
        temp_dir_parent = gettempdir()

    temp_dir = os.path.join(temp_dir_parent, "OpenJD")
    try:
        os.makedirs(temp_dir, mode=OPENJD_TEMPDIR_MODE, exist_ok=True)
    except OSError as err:
        raise RuntimeError(f"Could not create temporary directory {temp_dir}: {err}")

    # R5-3 fix: exist_ok=True accepts whatever is already at this predictable
    # path -- another local user's directory, or a symlink pointing elsewhere.
    # Validate and set the mode through one descriptor, so the entry cannot be
    # swapped between the two.
    _prepare_temp_dir_root(temp_dir)
    return temp_dir


class TempDir:
    """This class securely creates a temporary directory using the same rules as mkdtemp(),
    but with the option of having the directory owned by a user other than this process' user.

    Notes:
        posix - Only the group of the temp directory is set. The directory owner will be this
            process' uid. This process must be running as root to change the ownership, so we don't
            do it (don't really need to, either, since the use-case for this class is to
            create the Open Job Description Session working directory and that working directory needs to be
            both writable and deletable by this process).

    Trust precondition when ``user`` is given (POSIX):
        ``mkdtemp()``'s 0o700 is deliberately widened to 0o770 so that the target
        user can write into the session directory. That grant is to a *group*,
        not to a user, so **every member of the group named by the
        ``PosixSessionUser`` gains write access to the whole session tree** --
        not only the intended session user. Generated shell scripts and
        materialized embedded files live in that tree and are executed after
        being written, so a second member of that group could substitute their
        contents between materialization and exec.

        The caller is therefore required to supply a group that contains only
        principals it already trusts with the session's work -- conventionally a
        group dedicated to the pairing of this process' user and the one job
        user. This runtime cannot verify that and does not try to. The ordering
        here is careful about the part it can control: group ownership is changed
        *before* the mode is widened, so a failure to set the group never leaves
        a group-writable directory behind.

        The same precondition covers the group-writable bits that
        ``_embedded_files.write_file_for_user`` sets on individual files for the
        same reason.
    """

    path: Path
    """Pathname of the created directory.
    """

    def __init__(
        self,
        *,
        dir: Optional[Path] = None,
        prefix: Optional[str] = None,
        user: Optional[SessionUser] = None,
        logger: Optional[LoggerAdapter] = None,
    ):
        """
        Arguments:
            dir (Optional[Path]): The directory in which to create the temp dir.
                Defaults to tempfile.gettempdir().
            prefix (Optional[str]): A prefix to use in the name of the generated temp dir.
                Defaults to "".
            user (Optional[SessionUser]): A group that will own the created directory.
                The group-write bit will be set on the directory if this option is supplied.
                Defaults to this process' effective user/group.
            logger (Optional[LoggerAdapter]): The logger to which all messages should be sent from this and the
                subprocess.

        Raises:
            RuntimeError - If this process cannot create the temporary directory, or change the
                group ownership of the created directory.
        """
        # pre-flight checks
        if user and is_posix() and not isinstance(user, PosixSessionUser):  # pragma: nocover
            raise ValueError("user must be a posix-user. Got %s", type(user))
        elif user and is_windows() and not isinstance(user, WindowsSessionUser):
            raise ValueError("user must be a windows-user. Got %s", type(user))

        if not dir:
            dir = Path(custom_gettempdir(logger))

        dir = dir.resolve()
        try:
            self.path = Path(mkdtemp(dir=dir, prefix=prefix))  # 0o700
        except OSError as err:
            raise RuntimeError(f"Could not create temp directory within {str(dir)}: {str(err)}")

        # Change the owner
        if user:
            if is_posix():
                user = cast(PosixSessionUser, user)
                # Change ownership
                try:
                    chown(self.path, group=user.group)
                except OSError as err:
                    raise RuntimeError(
                        f"Could not change ownership of directory '{str(dir)}' (error: {str(err)}). Please ensure that uid {os.geteuid()} is a member of group {user.group}."  # type: ignore
                    )
                # Update the permissions to include the group after the group is changed
                # Note: Only after changing group for security in case the group-ownership
                # change fails.
                os.chmod(self.path, mode=stat.S_IRWXU | stat.S_IRWXG)
            elif is_windows():
                user = cast(WindowsSessionUser, user)
                try:
                    WindowsPermissionHelper.set_permissions(
                        str(self.path),
                        principals_full_control=[get_process_user()],
                        principals_modify_access=[user.user],
                    )
                except Exception as err:
                    raise RuntimeError(
                        f"Could not change permissions of directory '{str(dir)}' (error: {str(err)})"
                    )

    def cleanup(self) -> None:
        """Deletes the temporary directory and all of its contents.
        Raises:
            RuntimeError - If not all files could be deleted. The message names
                each path that could not be deleted along with the reason.
        """
        failures: list[str] = []

        def _record(path: Any, error: BaseException) -> None:
            # R5-7 fix: keep the reason. Cleanup is exactly where the difference
            # between "permission denied" and "a process still holds this open"
            # decides what the operator should do next, and the old handler
            # accepted the exception and discarded it -- leaving a list of bare
            # paths with no cause.
            failures.append(f"{path}: {error!r}")

        if sys.version_info >= (3, 12):
            # `onerror` is deprecated from 3.12 and slated for removal; `onexc`
            # receives the exception instance directly.
            def onexc(
                func: Any, path: Any, error: BaseException
            ) -> None:  # pragma: nocover - version-gated
                _record(path, error)

            rmtree(self.path, onexc=onexc)
        else:

            def onerror(
                func: Any, path: Any, exc_info: Any
            ) -> None:  # pragma: nocover - version-gated
                # Pre-3.12 handlers are called with sys.exc_info()-style triples.
                error = exc_info[1] if isinstance(exc_info, tuple) else exc_info
                _record(path, error)

            rmtree(self.path, onerror=onerror)

        if failures:
            raise RuntimeError(
                f"Files within temporary directory {str(self.path)} could not be deleted.\n"
                + "\n".join(failures)
            )
