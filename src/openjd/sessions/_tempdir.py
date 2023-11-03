# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
from pathlib import Path
from shutil import chown, rmtree
from tempfile import gettempdir, mkdtemp
from typing import Optional, cast

from ._session_user import PosixSessionUser, SessionUser, WindowsSessionUser
from ._os_checker import is_posix, is_windows

if is_windows():
    import win32security
    import ntsecuritycon


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
            dir = Path(gettempdir())

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
                    # Change permissions
                    if user.group:
                        principal_to_permit = user.group
                    else:
                        principal_to_permit = user.user

                    principal_sid, _, _ = win32security.LookupAccountName(None, principal_to_permit)

                    dacl = win32security.ACL()
                    dacl.AddAccessAllowedAce(
                        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, principal_sid
                    )

                    sd = win32security.GetFileSecurity(
                        str(self.path), win32security.DACL_SECURITY_INFORMATION
                    )

                    # Arguments:
                    # 1. bDaclPresent = 1: Indicates that the DACL is present in the security descriptor.
                    #    If set to 0, this method ignores the provided DACL and allows access to all principals.
                    # 2. dacl: The discretionary access control list (DACL) to be set in the security descriptor.
                    # 3. bDaclDefaulted = 0: Indicates the DACL was provided and not defaulted.
                    #    If set to 1, indicates the DACL was defaulted, as in the case of permissions inherited from a parent directory.
                    sd.SetSecurityDescriptorDacl(1, dacl, 0)

                    win32security.SetFileSecurity(
                        str(self.path), win32security.DACL_SECURITY_INFORMATION, sd
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
