# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
from ._logging import LoggerAdapter, LogContent, LogExtraInfo
from pathlib import Path
from shutil import chown, rmtree
from tempfile import gettempdir, mkdtemp
from typing import Optional, cast

from ._session_user import PosixSessionUser, SessionUser, WindowsSessionUser
from ._windows_permission_helper import WindowsPermissionHelper
from ._os_checker import is_posix, is_windows

if is_windows():
    from ._win32._helpers import get_process_user  # type: ignore


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
    os.makedirs(temp_dir, exist_ok=True)
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
            RuntimeError - If not all files could be deleted.
        """
        encountered_errors = False
        file_paths: list[str] = []

        def onerror(f, p, e):
            nonlocal encountered_errors
            nonlocal file_paths
            encountered_errors = True
            file_paths.append(str(p))

        rmtree(self.path, onerror=onerror)
        if encountered_errors:
            raise RuntimeError(
                f"Files within temporary directory {str(self.path)} could not be deleted.\n"
                + "\n".join(file_paths)
            )
