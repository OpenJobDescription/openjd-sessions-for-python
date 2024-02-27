# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock
from openjd.sessions._os_checker import is_posix, is_windows
import pytest

from utils.windows_acl_helper import principal_has_full_control_of_object

from openjd.model import SymbolTable
from openjd.model.v2023_09 import DataString as DataString_2023_09
from openjd.model.v2023_09 import (
    EmbeddedFileText as EmbeddedFileText_2023_09,
)
from openjd.model.v2023_09 import (
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)
from openjd.sessions._embedded_files import EmbeddedFiles, EmbeddedFilesScope
from openjd.sessions._session_user import PosixSessionUser, WindowsSessionUser

from .conftest import (
    has_posix_target_user,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path")
class TestEmbeddedFiles:
    class TestGetSymtabEntry:
        """Tests for EmbeddedFiles._get_symtab_entry().
        Note: Also tests EmbeddedFiles._find_value_prefix() indirectly.
        """

        @pytest.mark.parametrize(
            "scope,expected_symbol",
            [
                pytest.param(
                    EmbeddedFilesScope.STEP,
                    "Task.File.Foo",
                    id="step scope",
                ),
                pytest.param(
                    EmbeddedFilesScope.ENV,
                    "Env.File.Foo",
                    id="environment scope",
                ),
            ],
        )
        def test_given_filename(
            self, scope: EmbeddedFilesScope, expected_symbol: str, tmp_path: Path
        ) -> None:
            # Test that we just use the given filename for the file, and that we don't create the file.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=scope, session_files_directory=tmp_path
            )
            filename = "test_filename.txt"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                filename=filename,
                data=DataString_2023_09("some data"),
            )

            # WHEN
            result_symbol, result_filename = test_obj._get_symtab_entry(test_file)

            # THEN
            assert result_symbol == expected_symbol
            assert result_filename == tmp_path / filename
            assert not os.path.exists(result_filename)

        @pytest.mark.parametrize(
            "scope,expected_symbol",
            [
                pytest.param(
                    EmbeddedFilesScope.STEP,
                    "Task.File.Foo",
                    id="step scope",
                ),
                pytest.param(
                    EmbeddedFilesScope.ENV,
                    "Env.File.Foo",
                    id="environment scope",
                ),
            ],
        )
        def test_generate_filename(
            self, scope: EmbeddedFilesScope, expected_symbol: str, tmp_path: Path
        ) -> None:
            # Test that we generate a random filename under the given files directory,
            # and that the file exists.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=scope, session_files_directory=tmp_path
            )
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09("some data"),
            )

            # WHEN
            result_symbol, result_filename = test_obj._get_symtab_entry(test_file)

            # THEN
            assert result_symbol == expected_symbol
            assert os.path.exists(result_filename)
            assert result_filename.parent == tmp_path

    @pytest.mark.skipif(not is_posix(), reason="posix-specific test")
    class TestMaterializeFilePosix:
        """Tests for EmbeddedFiles._materialize_file() on posix systems.
        Note: Also tests EmbeddedFiles._find_value_prefix() indirectly.
        """

        def test_writes_file(self, tmp_path: Path) -> None:
            # Basic test -- make sure that we write the correct data to the file, and that
            #  the file permissions are set correctly.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.STEP, session_files_directory=tmp_path
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            statinfo = os.stat(filename)
            assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
            assert statinfo.st_gid == os.getegid(), "File group is this process' group"  # type: ignore
            assert statinfo.st_mode & stat.S_IRWXU == (stat.S_IRUSR | stat.S_IWUSR), "Owner has r/w"
            assert statinfo.st_mode & stat.S_IRWXG == 0, "Group has no permissions"
            assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

        def test_truncates_file(self, tmp_path: Path) -> None:
            # Make sure that when we write the embedded file that we open it truncated.
            # Else we'll have extra data at the end.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.STEP, session_files_directory=tmp_path
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()
            with open(filename, "w") as file:
                file.write("This needs to be longer than our test data to test truncation")

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

        def test_writes_file_runnable(self, tmp_path: Path) -> None:
            # As test_writes_file() but also setting the execute bit on the file.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.STEP, session_files_directory=tmp_path
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
                runnable=True,
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            statinfo = os.stat(filename)
            assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
            assert statinfo.st_gid == os.getegid(), "File group is this process' group"  # type: ignore
            assert statinfo.st_mode & stat.S_IRWXU == stat.S_IRWXU, "Owner has r/w/x"
            assert statinfo.st_mode & stat.S_IRWXG == 0, "Group has no permissions"
            assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

        def test_resolves_formatstring(self, tmp_path: Path) -> None:
            # Addition to the writes_file test that now ensures that the FormatStrings in the file
            # are correctly resolved.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.STEP, session_files_directory=tmp_path
            )
            testdata = "{{ Var.Value }}"
            testdataresult = "some data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
            )
            filename = tmp_path / uuid.uuid4().hex
            testdataresult = "some data"
            symtab = SymbolTable(source={"Var.Value": testdataresult})

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdataresult, "File contents are as expected"

        @pytest.mark.xfail(
            not has_posix_target_user(),
            reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
        )
        @pytest.mark.usefixtures("posix_target_user")
        def test_changes_owner(self, tmp_path: Path, posix_target_user: PosixSessionUser) -> None:
            # Test that the group of the file is properly changed when a user is given.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(),
                scope=EmbeddedFilesScope.STEP,
                session_files_directory=tmp_path,
                user=posix_target_user,
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()
            import grp

            gid = grp.getgrnam(posix_target_user.group).gr_gid  # type: ignore

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            statinfo = os.stat(filename)
            assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
            assert statinfo.st_gid == gid, "File group is the user's group"
            assert statinfo.st_mode & stat.S_IRWXU == (stat.S_IRUSR | stat.S_IWUSR), "Owner has r/w"
            assert statinfo.st_mode & stat.S_IRWXG == (stat.S_IRGRP | stat.S_IWGRP), "Group has r/w"
            assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

        @pytest.mark.xfail(
            not has_posix_target_user(),
            reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
        )
        @pytest.mark.usefixtures("posix_target_user")
        def test_changes_owner_runnable(
            self, tmp_path: Path, posix_target_user: PosixSessionUser
        ) -> None:
            # As test_changes_owner(), but also checks that the group execute bit is set if the file is runnable.

            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(),
                scope=EmbeddedFilesScope.STEP,
                session_files_directory=tmp_path,
                user=posix_target_user,
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
                runnable=True,
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()
            import grp

            gid = grp.getgrnam(posix_target_user.group).gr_gid  # type: ignore

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            statinfo = os.stat(filename)
            assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
            assert statinfo.st_gid == gid, "File group is the user's group"
            assert statinfo.st_mode & stat.S_IRWXU == stat.S_IRWXU, "Owner has r/w/x"
            assert statinfo.st_mode & stat.S_IRWXG == stat.S_IRWXG, "Group has r/w/x"
            assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

    @pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
    class TestMaterializeFileWindows:

        @pytest.mark.xfail(
            not has_windows_user(),
            reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
        )
        def test_changes_owner(self, tmp_path: Path, windows_user: WindowsSessionUser) -> None:
            # GIVEN
            test_obj = EmbeddedFiles(
                logger=MagicMock(),
                scope=EmbeddedFilesScope.STEP,
                session_files_directory=tmp_path,
                user=windows_user,
            )
            testdata = "some text data"
            test_file = EmbeddedFileText_2023_09(
                name="Foo",
                type=EmbeddedFileTypes_2023_09.TEXT,
                data=DataString_2023_09(testdata),
            )
            filename = tmp_path / uuid.uuid4().hex
            symtab = SymbolTable()

            # WHEN
            test_obj._materialize_file(filename, test_file, symtab)

            # THEN
            assert os.path.exists(filename)
            assert principal_has_full_control_of_object(
                str(filename), windows_user.user
            ), "Windows user has full control"
            with open(filename, "r") as file:
                result_contents = file.read()
            assert result_contents == testdata, "File contents are as expected"

    class TestMaterialize:
        """Tests for EmbeddedFiles.materialize()"""

        def test_basic(self, tmp_path: Path) -> None:
            # Basic test - we can write several files and they show up in the filesystem
            # where reported.

            # GIVEN
            @dataclass(frozen=True)
            class Datum:
                name: str
                data: str
                symbol: str
                runnable: bool = False

            symtab = SymbolTable()  # empty
            test_data: list[Datum] = [
                Datum(
                    data="foo's data",
                    symbol="Env.File.Foo",
                    name="Foo",
                ),
                Datum(
                    data="bar's data",
                    symbol="Env.File.Bar",
                    name="Bar",
                    runnable=True,
                ),
                Datum(
                    data="baz's data",
                    symbol="Env.File.Baz",
                    name="Baz",
                ),
            ]
            given_files = [
                EmbeddedFileText_2023_09(
                    name=f.name,
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data=DataString_2023_09(f.data),
                    runnable=f.runnable,
                )
                for f in test_data
            ]
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.ENV, session_files_directory=tmp_path
            )

            # WHEN
            test_obj.materialize(given_files, symtab)

            # THEN
            for data in test_data:
                assert data.symbol in symtab, f"Symbol for {data.name} is in the symtab"
                filename = symtab[data.symbol]
                assert os.path.exists(filename), f"File exists for {data.name}"
                with open(filename, "r") as file:
                    result_contents = file.read()
                assert result_contents == data.data, "File contents are as expected"
                # Check file permissions
                if is_posix():
                    statinfo = os.stat(filename)
                    assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
                    assert statinfo.st_gid == os.getegid(), "File group is this process' group"  # type: ignore
                    if data.runnable:
                        assert statinfo.st_mode & stat.S_IRWXU == stat.S_IRWXU, "Owner has r/w/x"
                    else:
                        assert statinfo.st_mode & stat.S_IRWXU == (
                            stat.S_IRUSR | stat.S_IWUSR
                        ), "Owner has r/w"
                    assert statinfo.st_mode & stat.S_IRWXG == 0, "Group has no permissions"
                    assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"

        @pytest.mark.skipif(not is_posix(), reason="posix-specific test")
        @pytest.mark.xfail(
            not has_posix_target_user(),
            reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
        )
        @pytest.mark.usefixtures("posix_target_user")
        def test_basic_as_user_posix(
            self, tmp_path: Path, posix_target_user: PosixSessionUser
        ) -> None:
            # Basic test - we can write several files and they show up in the filesystem
            # where reported.

            # GIVEN
            @dataclass(frozen=True)
            class Datum:
                name: str
                data: str
                symbol: str
                runnable: bool = False

            symtab = SymbolTable()  # empty
            test_data: list[Datum] = [
                Datum(
                    data="foo's data",
                    symbol="Env.File.Foo",
                    name="Foo",
                ),
                Datum(
                    data="bar's data",
                    symbol="Env.File.Bar",
                    name="Bar",
                    runnable=True,
                ),
                Datum(
                    data="baz's data",
                    symbol="Env.File.Baz",
                    name="Baz",
                ),
            ]
            given_files = [
                EmbeddedFileText_2023_09(
                    name=f.name,
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data=DataString_2023_09(f.data),
                    runnable=f.runnable,
                )
                for f in test_data
            ]
            test_obj = EmbeddedFiles(
                logger=MagicMock(),
                scope=EmbeddedFilesScope.ENV,
                session_files_directory=tmp_path,
                user=posix_target_user,
            )
            import grp

            gid = grp.getgrnam(posix_target_user.group).gr_gid  # type: ignore

            # WHEN
            test_obj.materialize(given_files, symtab)

            # THEN
            for data in test_data:
                assert data.symbol in symtab, f"Symbol for {data.name} is in the symtab"
                filename = symtab[data.symbol]
                assert os.path.exists(filename), f"File exists for {data.name}"
                with open(filename, "r") as file:
                    result_contents = file.read()
                assert result_contents == data.data, "File contents are as expected"
                # Check file permissions
                statinfo = os.stat(filename)
                assert statinfo.st_uid == os.geteuid(), "File owner is this process's owner"  # type: ignore
                assert statinfo.st_gid == gid, "File group is the user's group"
                if data.runnable:
                    assert statinfo.st_mode & stat.S_IRWXU == stat.S_IRWXU, "Owner has r/w/x"
                    assert statinfo.st_mode & stat.S_IRWXG == stat.S_IRWXG, "Group has r/w/x"
                else:
                    assert statinfo.st_mode & stat.S_IRWXU == (
                        stat.S_IRUSR | stat.S_IWUSR
                    ), "Owner has r/w"
                    assert statinfo.st_mode & stat.S_IRWXG == (
                        stat.S_IRGRP | stat.S_IWGRP
                    ), "Group has r/w"
                assert statinfo.st_mode & stat.S_IRWXO == 0, "Others have no permissions"

        @pytest.mark.skipif(not is_windows(), reason="Windows-specific test")
        @pytest.mark.xfail(
            not has_windows_user(),
            reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
        )
        def test_basic_as_user_windows(
            self, tmp_path: Path, windows_user: WindowsSessionUser
        ) -> None:
            # Basic test - we can write several files and they show up in the filesystem
            # where reported.

            # GIVEN
            @dataclass(frozen=True)
            class Datum:
                name: str
                data: str
                symbol: str
                runnable: bool = False

            symtab = SymbolTable()  # empty
            test_data: list[Datum] = [
                Datum(
                    data="foo's data",
                    symbol="Env.File.Foo",
                    name="Foo",
                ),
                Datum(
                    data="bar's data",
                    symbol="Env.File.Bar",
                    name="Bar",
                    runnable=True,
                ),
                Datum(
                    data="baz's data",
                    symbol="Env.File.Baz",
                    name="Baz",
                ),
            ]
            given_files = [
                EmbeddedFileText_2023_09(
                    name=f.name,
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data=DataString_2023_09(f.data),
                    runnable=f.runnable,
                )
                for f in test_data
            ]
            test_obj = EmbeddedFiles(
                logger=MagicMock(),
                scope=EmbeddedFilesScope.ENV,
                session_files_directory=tmp_path,
                user=windows_user,
            )

            # WHEN
            test_obj.materialize(given_files, symtab)

            # THEN
            for data in test_data:
                assert data.symbol in symtab, f"Symbol for {data.name} is in the symtab"
                filename = symtab[data.symbol]
                assert os.path.exists(filename), f"File exists for {data.name}"
                with open(filename, "r") as file:
                    result_contents = file.read()
                assert result_contents == data.data, "File contents are as expected"
                # Check file permissions
                assert principal_has_full_control_of_object(
                    filename, windows_user.user
                ), "Windows user has full control"

        def test_resolves_symbols(self, tmp_path: Path) -> None:
            # Tests that the set of files can reference themselves and each other
            # in their file data, and that we write the correct data.

            # GIVEN
            @dataclass(frozen=True)
            class Datum:
                name: str
                symbol: str
                filename: str

            symtab = SymbolTable(source={"Given.Symbol": "Symbol"})  # empty
            test_data: list[Datum] = [
                Datum(
                    symbol="Env.File.Foo",
                    name="Foo",
                    filename="foo.txt",
                ),
                Datum(
                    symbol="Env.File.Bar",
                    name="Bar",
                    filename="bar.txt",
                ),
                Datum(
                    symbol="Env.File.Baz",
                    name="Baz",
                    filename="baz.txt",
                ),
            ]
            # We'll put the same data in each file. That data will reference
            # all symbols that should exist in the symbol table. If we construct the
            # symbol table at the correct time within materialize, then we'll get the
            # expected result.
            given_file_data: str = f"""
            {{{{ Given.Symbol }}}}
            {{{{ {test_data[0].symbol} }}}}
            {{{{ {test_data[1].symbol} }}}}
            {{{{ {test_data[2].symbol} }}}}
            """
            expected_file_data: str = f"""
            Symbol
            {str(tmp_path / test_data[0].filename)}
            {str(tmp_path / test_data[1].filename)}
            {str(tmp_path / test_data[2].filename)}
            """
            given_files = [
                EmbeddedFileText_2023_09(
                    name=f.name,
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data=DataString_2023_09(given_file_data),
                    filename=f.filename,
                )
                for f in test_data
            ]
            test_obj = EmbeddedFiles(
                logger=MagicMock(), scope=EmbeddedFilesScope.ENV, session_files_directory=tmp_path
            )

            # WHEN
            test_obj.materialize(given_files, symtab)

            # THEN
            for data in test_data:
                assert data.symbol in symtab, f"Symbol for {data.name} is in the symtab"
                filename = symtab[data.symbol]
                assert os.path.exists(filename), f"File exists for {data.name}"
                with open(filename, "r") as file:
                    result_contents = file.read()
                assert result_contents == expected_file_data, "File contents are as expected"
