# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A ``PATH`` parameter's ``Param.*``/``Task.Param.*`` value must render in the
host's path format.

The session is host scope, and the Expression Language spec says a ``path`` there
takes the host operating system's semantics -- separators included. openjd-rs
does that unconditionally when it builds the session symbol table: every
``Param.<PATH>`` and ``Task.Param.<PATH>`` is wrapped in
``ExprValue::new_path(mapped, PathFormat::host())`` (``crates/openjd-sessions/
src/session.rs``), so the format is applied whether or not a path mapping rule
matched.

This package used to apply the host's separators only *inside*
``PathMappingRule.apply``, so a parameter with no matching rule reached the task
in whatever form the submitter wrote it. On a Windows host a POSIX-spelled
parameter stayed POSIX-spelled, which is what conformance fixture
``2023-09/base/jobs/3.4--path-parameter.test.yaml`` catches:

    output_windows:
    - TASK:InputFile=\\path\\a.exr

On simulating a Windows host. A POSIX host renders both readings identically, so
comparing values on this machine proves nothing -- the assertions would pass
whatever the code did. ``_windows_host`` forces the other format.

It patches exactly one seam, ``openjd.sessions._path_mapping.os_name``, and that
is deliberate rather than a simplification: that module-level name is the single
place this package decides which separator a host-scope path uses. It is what
``PathMappingRule.apply`` reads for the separator of a rule's output, and it is
what ``host_path_format`` reads for the format of an unmapped value. Patching one
and not the other would produce a self-inconsistent host -- mapped values
rendering Windows while unmapped ones rendered POSIX -- and a test built on that
would assert an arrangement that cannot occur in production. Keeping both readers
on one seam is the reason the fix put ``host_path_format`` in ``_path_mapping``
rather than deriving ``os.name`` a second time in ``_session``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import PurePosixPath, PureWindowsPath
from typing import Generator
from unittest.mock import patch

import pytest

from openjd.model import (
    ParameterValue,
    ParameterValueType,
    SpecificationRevision,
    SymbolTable,
)
from openjd.sessions import PathFormat, PathMappingRule, Session

import openjd.sessions._path_mapping as path_mapping_impl_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POSIX_TEXT = "/path/a.exr"
"""The parameter as a submitter wrote it."""

_WINDOWS_TEXT = r"\path\a.exr"
"""The same parameter as a Windows host must render it. Distinct from
``_POSIX_TEXT``, which is what an unformatted value shows."""


@contextmanager
def _host(os_name: str) -> Generator[None, None, None]:
    """Force the host path format. See this module's docstring for why one seam
    is enough, and why it is this one."""
    with patch.object(path_mapping_impl_mod, "os_name", os_name):
        yield


def _symtab(
    params: dict[str, ParameterValue],
    *,
    os_name: str,
    rules: list[PathMappingRule] | None = None,
) -> SymbolTable:
    """Build a session symbol table the way a running session does."""
    with Session(
        session_id="test-path-format",
        job_parameter_values=params,
        path_mapping_rules=rules or [],
    ) as session:
        with _host(os_name):
            return session._symbol_table(SpecificationRevision.v2023_09, params)


def _path_param(value: str) -> dict[str, ParameterValue]:
    return {"InputFile": ParameterValue(type=ParameterValueType.PATH, value=value)}


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


class TestPathParameterTakesTheHostFormat:
    """``Param.*``/``Task.Param.*`` for a PATH parameter render in the host's
    format, with no path mapping rule involved."""

    @pytest.mark.parametrize("key", ["Param.InputFile", "Task.Param.InputFile"])
    def test_windows_host_renders_backslashes(self, key: str) -> None:
        # GIVEN a POSIX-spelled PATH parameter and no path mapping rules
        # WHEN the session symbol table is built on a Windows host
        symtab = _symtab(_path_param(_POSIX_TEXT), os_name="nt")

        # THEN the value carries the host's separators
        assert str(symtab[key]) == _WINDOWS_TEXT

    @pytest.mark.parametrize("key", ["Param.InputFile", "Task.Param.InputFile"])
    def test_posix_host_leaves_the_value_alone(self, key: str) -> None:
        """The negative control for the test above: a POSIX host must not
        rewrite anything, so a fix that always converts is caught here."""
        # GIVEN the same parameter
        # WHEN the table is built on a POSIX host
        symtab = _symtab(_path_param(_POSIX_TEXT), os_name="posix")

        # THEN it is unchanged
        assert str(symtab[key]) == _POSIX_TEXT

    @pytest.mark.parametrize("key", ["RawParam.InputFile", "Task.RawParam.InputFile"])
    def test_the_raw_form_is_not_reformatted(self, key: str) -> None:
        """openjd-rs passes a PATH parameter's raw value through untouched
        (``JobParameterType::Path | ListPath => param.value.clone()``), so the
        fix must not reach ``RawParam.*``. Without this, a fix that normalizes
        both forms looks correct on every other assertion here."""
        # GIVEN a POSIX-spelled PATH parameter
        # WHEN the table is built on a Windows host
        symtab = _symtab(_path_param(_POSIX_TEXT), os_name="nt")

        # THEN the raw form is still what the submitter wrote
        assert str(symtab[key]) == _POSIX_TEXT


class TestPathParameterFormatAndPathMappingAgree:
    """The host format is applied whether or not a rule matched. This is the
    property that ``PathMappingRule.apply`` alone cannot provide."""

    def test_a_non_matching_rule_still_leaves_a_host_format_value(self) -> None:
        """The defect in its narrowest form: a rule exists, so the mapping code
        runs, but it does not match, so its separator choice never applies."""
        # GIVEN a rule that cannot match the parameter
        rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/nowhere"),
                destination_path=PurePosixPath("/elsewhere"),
            )
        ]

        # WHEN the table is built on a Windows host
        symtab = _symtab(_path_param(_POSIX_TEXT), os_name="nt", rules=rules)

        # THEN the unmapped value still took the host's format
        assert str(symtab["Task.Param.InputFile"]) == _WINDOWS_TEXT

    def test_a_matching_rule_is_unchanged_by_the_fix(self) -> None:
        """A matching rule already emitted host separators, so normalizing after
        it must be a no-op rather than a second transformation."""
        # GIVEN a rule that matches
        rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/path"),
                destination_path=PureWindowsPath(r"C:\dest"),
            )
        ]

        # WHEN the table is built on a Windows host
        symtab = _symtab(_path_param(_POSIX_TEXT), os_name="nt", rules=rules)

        # THEN the mapped value is what path mapping produced, unaltered
        assert str(symtab["Task.Param.InputFile"]) == r"C:\dest\a.exr"


class TestPathParameterFormatIsSeparatorsOnly:
    """Guards the *shape* of the conversion. Rendering through
    ``PureWindowsPath``/``PurePosixPath`` would satisfy the assertions above and
    still be wrong: it collapses duplicate separators, drops a trailing
    separator, rewrites ``s3://`` to ``s3:/``, and turns an empty value into
    ``'.'``. openjd-rs replaces separators and nothing else
    (``normalize_path_separators``), so these pin that.
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            pytest.param("/a//b", r"\a\\b", id="duplicate separators survive"),
            pytest.param("/a/", "\\a\\", id="trailing separator survives"),
            pytest.param("relative/path", r"relative\path", id="relative path"),
            pytest.param("", "", id="empty value stays empty"),
            pytest.param(r"C:\already\windows", r"C:\already\windows", id="already windows"),
        ],
    )
    def test_windows_host_replaces_separators_only(self, given: str, expected: str) -> None:
        # GIVEN a PATH parameter whose text a PurePath would rewrite
        # WHEN the table is built on a Windows host
        symtab = _symtab(_path_param(given), os_name="nt")

        # THEN only the separators changed
        assert str(symtab["Task.Param.InputFile"]) == expected

    def test_a_uri_is_left_alone(self) -> None:
        """A ``path`` holding a URI keeps forward slashes on every host. The
        Expression Language spec makes URI paths exempt from the host's
        separator, and openjd-rs implements that in the same function the fix
        reaches (``is_uri`` short-circuits ``normalize_path_separators``)."""
        # GIVEN a PATH parameter holding a URI
        given = "s3://bucket/key/with/slashes"

        # WHEN the table is built on a Windows host
        symtab = _symtab(_path_param(given), os_name="nt")

        # THEN it is untouched
        assert str(symtab["Task.Param.InputFile"]) == given


class TestListPathParameterTakesTheHostFormat:
    """openjd-rs formats a ``LIST[PATH]`` element-wise. So must this."""

    def test_windows_host_formats_every_element(self) -> None:
        # GIVEN a LIST[PATH] parameter of POSIX-spelled elements
        params = {
            "Inputs": ParameterValue(
                type=ParameterValueType.LIST_PATH,
                value=["/path/a.exr", "/other/b.exr"],
            )
        }

        # WHEN the table is built on a Windows host
        symtab = _symtab(params, os_name="nt")

        # THEN every element carries the host's separators
        assert [str(element) for element in symtab["Task.Param.Inputs"]] == [
            r"\path\a.exr",
            r"\other\b.exr",
        ]

    def test_posix_host_leaves_every_element_alone(self) -> None:
        # GIVEN the same parameter
        params = {
            "Inputs": ParameterValue(
                type=ParameterValueType.LIST_PATH,
                value=["/path/a.exr", "/other/b.exr"],
            )
        }

        # WHEN the table is built on a POSIX host
        symtab = _symtab(params, os_name="posix")

        # THEN nothing changed
        assert [str(element) for element in symtab["Task.Param.Inputs"]] == [
            "/path/a.exr",
            "/other/b.exr",
        ]


class TestOtherParameterTypesAreUntouched:
    """The fix is scoped to PATH and LIST[PATH]. A STRING that happens to look
    like a path must not be reformatted -- openjd-rs coerces it as a string."""

    def test_a_string_parameter_keeps_its_slashes_on_a_windows_host(self) -> None:
        # GIVEN a STRING parameter whose value looks like a POSIX path
        params = {"Text": ParameterValue(type=ParameterValueType.STRING, value=_POSIX_TEXT)}

        # WHEN the table is built on a Windows host
        symtab = _symtab(params, os_name="nt")

        # THEN it is still the string the submitter wrote
        assert str(symtab["Param.Text"]) == _POSIX_TEXT
        assert str(symtab["Task.Param.Text"]) == _POSIX_TEXT
