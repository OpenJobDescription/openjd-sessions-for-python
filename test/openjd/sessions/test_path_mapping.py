# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from enum import Enum
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from unittest.mock import patch

import pytest

from openjd.sessions import PathFormat, PathMappingRule
from openjd.sessions import _path_mapping as path_mapping_impl_mod


class OSName(str, Enum):
    POSIX = "posix"
    WINDOWS = "nt"


class TestPathMapping:
    @pytest.mark.parametrize(
        "rule, dest_os, given, expected",
        [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.POSIX,
                    source_path=PurePosixPath("/mnt/shared/"),
                    destination_path=PurePosixPath("/newprefix"),
                ),
                OSName.POSIX,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                ("/mnt/shared", "/newprefix", "posix->posix: sourcepath"),
                ("/mnt/shared/", "/newprefix/", "posix->posix: sourcepath-trailing-slash"),
                ("/mnt/shared/file", "/newprefix/file", "posix->posix: 1-level file"),
                ("/mnt/shared/dir/", "/newprefix/dir/", "posix->posix: 1-level dir"),
                ("/mnt/shared/dir/file", "/newprefix/dir/file", "posix->posix: 2-level file"),
                ("/mnt/shared/dir/dir2/", "/newprefix/dir/dir2/", "posix->posix: 2-level dir"),
                (
                    "/mnt/shared/dir/../file",
                    "/newprefix/dir/../file",
                    "posix->posix: 2-level relative file",
                ),
                (
                    "/mnt/shared/dir/../dir2/",
                    "/newprefix/dir/../dir2/",
                    "posix->posix: 2-level relative dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.POSIX,
                    source_path=PurePosixPath("/mnt/shared/"),
                    destination_path=PureWindowsPath("c:\\newprefix"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                ("/mnt/shared", "c:\\newprefix", "posix->windows: sourcepath"),
                ("/mnt/shared/", "c:\\newprefix\\", "posix->windows: sourcepath-trailing-slash"),
                ("/mnt/shared/file", "c:\\newprefix\\file", "posix->windows: 1-level file"),
                ("/mnt/shared/dir/", "c:\\newprefix\\dir\\", "posix->windows: 1-level dir"),
                (
                    "/mnt/shared/dir/file",
                    "c:\\newprefix\\dir\\file",
                    "posix->windows: 2-level file",
                ),
                (
                    "/mnt/shared/dir/dir2/",
                    "c:\\newprefix\\dir\\dir2\\",
                    "posix->windows: 2-level dir",
                ),
                (
                    "/mnt/shared/dir/../file",
                    "c:\\newprefix\\dir\\..\\file",
                    "posix->windows: 2-level relative file",
                ),
                (
                    "/mnt/shared/dir/../dir2/",
                    "c:\\newprefix\\dir\\..\\dir2\\",
                    "posix->windows: 2-level relative dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("c:\\mnt\\shared\\"),
                    destination_path=PurePosixPath("/newprefix"),
                ),
                OSName.POSIX,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                ("c:\\mnt\\shared", "/newprefix", "windows->posix: sourcepath"),
                ("c:\\mnt\\shared\\", "/newprefix/", "windows->posix: sourcepath-trailing-slash"),
                ("c:\\mnt\\shared\\file", "/newprefix/file", "windows->posix: 1-level file"),
                ("c:\\mnt\\shared\\dir\\", "/newprefix/dir/", "windows->posix: 1-level dir"),
                (
                    "c:\\mnt\\shared\\dir\\file",
                    "/newprefix/dir/file",
                    "windows->posix: 2-level file",
                ),
                (
                    "c:\\mnt\\shared\\dir\\dir2\\",
                    "/newprefix/dir/dir2/",
                    "windows->posix: 2-level dir",
                ),
                (
                    "c:\\mnt\\shared\\dir\\..\\file",
                    "/newprefix/dir/../file",
                    "windows->posix: 2-level relative file",
                ),
                (
                    "c:\\mnt\\shared\\dir\\..\\dir2\\",
                    "/newprefix/dir/../dir2/",
                    "windows->posix: 2-level relative dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("c:\\mnt\\shared\\"),
                    destination_path=PureWindowsPath("c:\\newprefix"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                ("c:\\mnt\\shared", "c:\\newprefix", "windows->windows: sourcepath"),
                (
                    "c:\\mnt\\shared\\",
                    "c:\\newprefix\\",
                    "windows->windows: sourcepath-trailing-slash",
                ),
                ("c:\\mnt\\shared\\file", "c:\\newprefix\\file", "windows->windows: 1-level file"),
                ("c:\\mnt\\shared\\dir\\", "c:\\newprefix\\dir\\", "windows->windows: 1-level dir"),
                (
                    "c:\\mnt\\shared\\dir\\file",
                    "c:\\newprefix\\dir\\file",
                    "windows->windows: 2-level file",
                ),
                (
                    "c:\\mnt\\shared\\dir\\dir2\\",
                    "c:\\newprefix\\dir\\dir2\\",
                    "windows->windows: 2-level dir",
                ),
                (
                    "c:\\mnt\\shared\\dir\\..\\file",
                    "c:\\newprefix\\dir\\..\\file",
                    "windows->windows: 2-level relative file",
                ),
                (
                    "c:\\mnt\\shared\\dir\\..\\dir2\\",
                    "c:\\newprefix\\dir\\..\\dir2\\",
                    "windows->windows: 2-level relative dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("\\\\128.0.0.1\\share\\assets"),
                    destination_path=PureWindowsPath("z:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "\\\\128.0.0.1\\share\\assets\\file",
                    "z:\\assets\\file",
                    "windows->windows: from unc file",
                ),
                (
                    "\\\\128.0.0.1\\share\\assets\\dir\\",
                    "z:\\assets\\dir\\",
                    "windows->windows: from unc dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("z:\\assets"),
                    destination_path=PureWindowsPath("\\\\128.0.0.1\\share\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "z:\\assets\\file",
                    "\\\\128.0.0.1\\share\\assets\\file",
                    "windows->windows: to unc file",
                ),
                (
                    "z:\\assets\\dir\\",
                    "\\\\128.0.0.1\\share\\assets\\dir\\",
                    "windows->windows: to unc dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("\\\\.\\c:\\assets"),
                    destination_path=PureWindowsPath("z:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "\\\\.\\c:\\assets\\file",
                    "z:\\assets\\file",
                    "windows->windows: from dos device path dot file",
                ),
                (
                    "\\\\.\\c:\\assets\\dir\\",
                    "z:\\assets\\dir\\",
                    "windows->windows: from dos device path dot dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("z:\\assets"),
                    destination_path=PureWindowsPath("\\\\.\\c:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "z:\\assets\\file",
                    "\\\\.\\c:\\assets\\file",
                    "windows->windows: to dos device path dot file",
                ),
                (
                    "z:\\assets\\dir\\",
                    "\\\\.\\c:\\assets\\dir\\",
                    "windows->windows: to dos device path dot dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("\\\\?\\c:\\assets"),
                    destination_path=PureWindowsPath("z:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "\\\\?\\c:\\assets\\file",
                    "z:\\assets\\file",
                    "windows->windows: from dos device path ? file",
                ),
                (
                    "\\\\?\\c:\\assets\\dir\\",
                    "z:\\assets\\dir\\",
                    "windows->windows: from dos device path ? dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("z:\\assets"),
                    destination_path=PureWindowsPath("\\\\?\\c:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "z:\\assets\\file",
                    "\\\\?\\c:\\assets\\file",
                    "windows->windows: to dos device path ? file",
                ),
                (
                    "z:\\assets\\dir\\",
                    "\\\\?\\c:\\assets\\dir\\",
                    "windows->windows: to dos device path ? dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath(
                        "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets"
                    ),
                    destination_path=PureWindowsPath("z:\\assets"),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets\\file",
                    "z:\\assets\\file",
                    "windows->windows: from dos device path volume file",
                ),
                (
                    "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets\\dir\\",
                    "z:\\assets\\dir\\",
                    "windows->windows: from dos device path volume dir",
                ),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("z:\\assets"),
                    destination_path=PureWindowsPath(
                        "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets"
                    ),
                ),
                OSName.WINDOWS,
                source,
                dest,
                id=id,
            )
            for source, dest, id in (
                (
                    "z:\\assets\\file",
                    "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets\\file",
                    "windows->windows: to dos device path volume file",
                ),
                (
                    "z:\\assets\\dir\\",
                    "\\\\?\\Volume{b75e2c83-0000-0000-0000-602f12345678}\\assets\\dir\\",
                    "windows->windows: to dos device path volume dir",
                ),
            )
        ],
    )
    def test_remaps(
        self, rule: PathMappingRule, dest_os: OSName, given: str, expected: str
    ) -> None:
        # Test that the given path is modified to the expected form

        # WHEN
        with patch(f"{path_mapping_impl_mod.__name__}.os_name", dest_os.value):
            changed, result = rule.apply(path=given)

        # THEN
        assert changed
        assert result == expected

    @pytest.mark.parametrize(
        "rule, given",
        [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.POSIX,
                    source_path=PurePosixPath("/mnt/shared"),
                    destination_path=PureWindowsPath("c:\\newprefix"),
                ),
                path,
                id=id,
            )
            for path, id in (
                ("/mnt", "posix: parent dir"),
                ("/mnt/share", "posix: different dir too short"),
                ("/mnt/shared2", "posix: different dir same prefix"),
            )
        ]
        + [
            pytest.param(
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("c:\\mnt\\shared\\"),
                    destination_path=PureWindowsPath("c:\\newprefix"),
                ),
                path,
                id=id,
            )
            for path, id in (
                ("c:\\mnt", "windows: parent dir"),
                ("c:\\mnt\\share", "windows: different dir too short"),
                ("c:\\mnt\\shared2", "windows: different dir same prefix"),
            )
        ],
    )
    def test_does_not_remap(self, rule: PathMappingRule, given: str) -> None:
        # Test that the given path is not modified by the path mapping rule

        # WHEN
        changed, result = rule.apply(path=given)

        # THEN
        assert not changed
        assert result == given

    @pytest.mark.parametrize(
        "rule_params",
        [
            {
                "source_path_format": PathFormat.WINDOWS,
                "source_path": PurePosixPath("C:\\oldprefix"),
                "destination_path": PureWindowsPath("c:\\newprefix"),
            },
            {
                "source_path_format": PathFormat.POSIX,
                "source_path": PureWindowsPath("/mnt/oldprefix"),
                "destination_path": PureWindowsPath("c:\\newprefix"),
            },
        ],
    )
    def test_mismatching_source_path_format_path(self, rule_params):
        with pytest.raises(ValueError):
            PathMappingRule(**rule_params)

    @pytest.mark.parametrize(
        "dict_rule, expected",
        [
            (
                {
                    "source_path_format": "WINDOWS",
                    "source_path": "C:\\oldprefix",
                    "destination_path": "c:\\newprefix",
                },
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("C:\\oldprefix"),
                    destination_path=PurePath("c:\\newprefix"),
                ),
            ),
            (
                {
                    "source_path_format": "POSIX",
                    "source_path": "/mnt/oldprefix",
                    "destination_path": "c:\\newprefix",
                },
                PathMappingRule(
                    source_path_format=PathFormat.POSIX,
                    source_path=PurePosixPath("/mnt/oldprefix"),
                    destination_path=PurePath("c:\\newprefix"),
                ),
            ),
            (
                {
                    "source_path_format": "windows",
                    "source_path": "C:\\oldprefix",
                    "destination_path": "c:\\newprefix",
                },
                PathMappingRule(
                    source_path_format=PathFormat.WINDOWS,
                    source_path=PureWindowsPath("C:\\oldprefix"),
                    destination_path=PurePath("c:\\newprefix"),
                ),
            ),
            (
                {
                    "source_path_format": "posix",
                    "source_path": "/mnt/oldprefix",
                    "destination_path": "c:\\newprefix",
                },
                PathMappingRule(
                    source_path_format=PathFormat.POSIX,
                    source_path=PurePosixPath("/mnt/oldprefix"),
                    destination_path=PurePath("c:\\newprefix"),
                ),
            ),
        ],
    )
    def test_from_dict_success(self, dict_rule, expected):
        rule = PathMappingRule.from_dict(dict_rule)
        assert rule == expected

    @pytest.mark.parametrize(
        "dict_rule",
        [
            (
                {
                    "source_path_format": "WINDOWS10",
                    "source_path": "C:\\oldprefix",
                    "destination_path": "c:\\newprefix",
                }
            ),
            ({"source_path": "/mnt/oldprefix", "destination_path": "c:\\newprefix"}),
            ({"source_path_format": "POSIX", "destination_path": "c:\\newprefix"}),
            ({"source_path_format": "POSIX", "source_path": "/mnt/oldprefix"}),
            (
                {
                    "source_path_format": "windows",
                    "source_path": "C:\\oldprefix",
                    "destination_path": "c:\\newprefix",
                    "extra_field": "value",
                }
            ),
        ],
    )
    def test_from_dict_failure(self, dict_rule):
        with pytest.raises(ValueError):
            PathMappingRule.from_dict(dict_rule)
