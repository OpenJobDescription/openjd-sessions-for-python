# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from shutil import chown
from tempfile import mkstemp
from typing import Any, Generator, Optional, cast

from openjd.model import SymbolTable, FormatStringError
from openjd.model.v2023_09 import EmbeddedFileText as EmbeddedFileText_2023_09
from openjd.model.v2023_09 import (
    ValueReferenceConstants as ValueReferenceConstants_2023_09,
)
from ._logging import LoggerAdapter, LogExtraInfo, LogContent
from ._session_user import PosixSessionUser, SessionUser, WindowsSessionUser
from ._types import EmbeddedFilesListType, EmbeddedFileType

from ._windows_permission_helper import WindowsPermissionHelper
from ._os_checker import is_windows

if is_windows():
    from ._win32._helpers import get_process_user  # type: ignore

__all__ = ("EmbeddedFilesScope", "EmbeddedFiles")


@contextmanager
def _open_context(*args: Any, **kwargs: Any) -> Generator[int, None, None]:
    fd = os.open(*args, **kwargs)
    try:
        yield fd
    finally:
        os.close(fd)


def write_file_for_user(
    filename: Path, data: str, user: Optional[SessionUser], additional_permissions: int = 0
) -> None:
    # File should only be r/w by the owner, by default

    # flags:
    #  O_WRONLY - open for writing
    #  O_CREAT - create if it does not exist
    #  O_TRUNC - truncate the file. If we overwrite an existing file, then we
    #            need to clear its contents.
    #  O_EXCL (intentionally not present) - fail if file exists
    #    - We exclude this 'cause we expect to be writing the same embedded file
    #      into the same location repeatedly with different contents as we run
    #      multiple Tasks in the same Session.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    # mode:
    #  S_IRUSR - Read by owner
    #  S_IWUSR - Write by owner
    mode = stat.S_IRUSR | stat.S_IWUSR | (additional_permissions & stat.S_IRWXU)
    with _open_context(filename, flags, mode=mode) as fd:
        os.write(fd, data.encode("utf-8"))

    if os.name == "posix":
        if user is not None:
            user = cast(PosixSessionUser, user)
            # Set the group of the file
            chown(filename, group=user.group)
            # Update the permissions to include the group after the group is changed
            # Note: Only after changing group for security in case the group-ownership
            # change fails.
            mode |= stat.S_IRGRP | stat.S_IWGRP | (additional_permissions & stat.S_IRWXG)

        # The file may have already existed before calling this function (e.g. created by mkstemp)
        # so unconditionally set the file permissions to ensure that additional_permissions are set.
        os.chmod(filename, mode=mode)

    elif os.name == "nt":
        if user is not None:
            user = cast(WindowsSessionUser, user)
            process_user = get_process_user()
            WindowsPermissionHelper.set_permissions(
                str(filename),
                principals_full_control=[process_user],
                principals_modify_access=[user.user],
            )


class EmbeddedFilesScope(Enum):
    """What scope of Script a given set of files is for.
    This dictates what prefix is used in format string variables
    """

    STEP = "step"
    ENV = "environment"


@dataclass(frozen=True)
class _FileRecord:
    symbol: str
    filename: Path
    file: EmbeddedFileType


# Note: "EmbeddedFiles" is currently "Attachments" in the Open Job Description template, but that
# will be changing to "EmbeddedFiles" to eliminate potential confusion with job bundle's
# "attachments"
class EmbeddedFiles:
    """Functionality for materializing a Script's Embedded Files to disk, and
    adding their values to a SymbolTable for use in the Script's Actions.
    """

    def __init__(
        self,
        *,
        logger: LoggerAdapter,
        scope: EmbeddedFilesScope,
        session_files_directory: Path,
        user: Optional[SessionUser] = None,
    ) -> None:
        """
        Arguments:
            logger (LoggerAdapter): Logger to send any logging messages to (e.g. errors).
            scope (EmbeddedFilesKind): The scope of the embedded files (used to determine
                value reference prefix in Format Strings).
            session_files_directory (Path): Directory within which to materialize the files to disk.
            user (Optional[SessionUser]): A group that will own the created files.
                The group rw bits will be set on the file if this option is supplied.
                Defaults to current user.
        """
        self._logger = logger
        self._scope = scope
        self._target_directory = session_files_directory
        self._user = user

    def materialize(self, files: EmbeddedFilesListType, symtab: SymbolTable) -> None:
        if self._scope == EmbeddedFilesScope.ENV:
            self._logger.info("Writing embedded files for Environment to disk.")
        else:
            self._logger.info("Writing embedded files for Task to disk.")

        try:
            records = list[_FileRecord]()
            # Generate the symbol table values and filenames
            for file in files:
                # Raises: OSError
                symbol, filename = self._get_symtab_entry(file)
                records.append(_FileRecord(symbol=symbol, filename=filename, file=file))

            # Add symbols to the symbol table
            for record in records:
                symtab[record.symbol] = str(record.filename)
                self._logger.info(
                    f"Mapping: {record.symbol} -> {record.filename}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.FILE_PATH | LogContent.PARAMETER_INFO
                    ),
                )

            # Write the files to disk.
            for record in records:
                # Raises: OSError
                self._materialize_file(record.filename, record.file, symtab)
        except OSError as err:
            raise RuntimeError(f"Could not write embedded file: {err}")
        except FormatStringError as err:
            # This should *never* happen. All format string contents are
            # checked when building the Job Template model. If we get here,
            # then something is broken with our model validation.
            raise RuntimeError(f"Error resolving format string: {str(err)}")

    def _find_value_prefix(self, file: EmbeddedFileType) -> str:
        """Figure out what prefix to use when referencing the file in format strings.
        We figure this out based on the model that `file` comes from and
        self._scope.
        """
        # When adding a new schema, start this method with a check for which
        # model 'file' belongs to -- that'll tell us the schema version.
        assert isinstance(file, EmbeddedFileText_2023_09)

        if self._scope == EmbeddedFilesScope.ENV:
            return ValueReferenceConstants_2023_09.ENV_FILE_PREFIX.value
        else:
            return ValueReferenceConstants_2023_09.TASK_FILE_PREFIX.value

    def _get_symtab_entry(self, file: EmbeddedFileType) -> tuple[str, Path]:
        """Figure out the entry to add to the symbol table for the given
        file. The value of the symbol table entry is the absolute filename
        of the file that we manifest on disk.

        Note: If a random filename is generated, then this does create
           the file as empty to reserve the filename on the filesystem.

        Returns:
            (symbol, value):
                symbol - The symbol to add to the symbol table.
                value - The absolute filename of the file to manifest.
        """

        assert isinstance(file, EmbeddedFileText_2023_09)

        # Figure out what filename to use for the given embedded file.
        # This will either be provided in the given 'file' or we will
        # randomly generate one.
        filename: Path
        if not file.filename:
            # Raises: OSError
            fd, fname = mkstemp(dir=self._target_directory)  # 0o600
            os.close(fd)
            filename = Path(fname)
        else:
            filename = self._target_directory / file.filename

        return (f"{self._find_value_prefix(file)}.{file.name}", filename)

    def _materialize_file(
        self, filename: Path, file: EmbeddedFileType, symtab: SymbolTable
    ) -> None:
        """Materialize/write the file data to disk.
        If self._user is set, then make it r/w by the given group.
        Make the file executable if the file settings indicate that we should.
        """

        assert isinstance(file, EmbeddedFileText_2023_09)

        execute_permissions = 0
        if file.runnable:
            # Allow the owner to execute the file and the group if self._user is set
            execute_permissions |= stat.S_IXUSR | (stat.S_IXGRP if self._user is not None else 0)

        data = file.data.resolve(symtab=symtab)
        # Create the file as r/w owner, and optionally group
        write_file_for_user(filename, data, self._user, additional_permissions=execute_permissions)

        self._logger.info(
            f"Wrote: {file.name} -> {str(filename)}",
            extra=LogExtraInfo(openjd_log_content=LogContent.FILE_PATH),
        )
        self._logger.debug(
            "Contents:\n%s", data, extra=LogExtraInfo(openjd_log_content=LogContent.FILE_CONTENTS)
        )
