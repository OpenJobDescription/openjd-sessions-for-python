# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import re
from dataclasses import dataclass, fields
from enum import Enum
from os import name as os_name
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from string import ascii_lowercase, ascii_uppercase
from typing import Optional, Union

_ASCII_LOWER_TABLE = str.maketrans(ascii_uppercase, ascii_lowercase)
"""Translation table for an ASCII-only case fold.

RFC 3986 makes a URI's scheme and authority case-insensitive over ASCII only,
which is what openjd-rs implements (``str::eq_ignore_ascii_case``). ``str.lower()``
would additionally fold non-ASCII characters — U+212A KELVIN SIGN lowers to ASCII
``k``, so ``s3://bucket\u212a`` would match ``s3://bucketk`` here but not in
openjd-rs. A translation table is also safer than ``str.lower()``/``str.casefold()``
because it cannot change the string's length (``"\u0130".lower()`` yields two
characters), and :meth:`PathMappingRule._apply_uri` relies on offsets into the
string."""


def _ascii_lower(value: str) -> str:
    """Lower-case the ASCII letters in ``value``, leaving everything else alone."""
    return value.translate(_ASCII_LOWER_TABLE)


def to_host_path_separators(value: str) -> str:
    """Render ``value`` with this host's path separators.

    A session is host scope, so a ``path`` there takes the host operating
    system's semantics. openjd-rs applies that to every host-scope path value by
    constructing it through ``ExprValue::new_path(.., PathFormat::host())``,
    which calls ``normalize_path_separators``
    (``crates/openjd-expr/src/value.rs``). This is that function for the host's
    format, and the three cases are its three arms:

    - a URI keeps forward slashes on every host, because its path portion is a
      set of opaque identifiers rather than a filesystem path;
    - a POSIX host changes nothing, because a backslash is a legal character in
      a POSIX filename and rewriting one would corrupt the path;
    - a Windows host replaces ``/`` with ``\\``.

    Separators and nothing else. Rendering through ``PureWindowsPath`` would
    also collapse ``//`` to ``\\``, drop a trailing separator, and turn ``""``
    into ``"."`` -- and :meth:`PathMappingRule.apply` deliberately preserves a
    trailing separator, so that one is a behaviour this must not undo.

    Duplicated from Rust rather than called through ``openjd.expr``: the native
    extension must not become a load-time requirement of a non-EXPR session (see
    ``test/openjd/test_import_purity.py``), and a PATH parameter reaches this on
    the non-EXPR path. The URI test reuses :data:`_URI_SOURCE_RE`, which is this
    module's existing spelling of the same ``<scheme>://`` rule, so the
    duplication is of Rust's three-way branch only.
    """
    if _URI_SOURCE_RE.match(value) is not None:
        return value
    if os_name == "posix":
        return value
    return value.replace("/", "\\")


_URI_SOURCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
"""A URI-format rule's ``source_path`` must start with ``<scheme>://``.

Mirrors the scheme grammar of RFC 3986 §3.1, which is what the EXPR engine's
URI parser accepts. Validated in the constructor so a malformed rule is rejected
where the value can be named."""


class PathFormat(str, Enum):
    POSIX = "POSIX"
    WINDOWS = "WINDOWS"
    # RFC 0006 §2.3.2 (EXPR extension): URI-form source paths
    # (e.g. "s3://bucket/prefix") that map to local filesystem paths.
    URI = "URI"


@dataclass(frozen=True)
class PathMappingRule:
    source_path_format: PathFormat
    # URI-format rules keep the source as the raw string: URIs are not
    # filesystem paths, and PurePath normalization would corrupt the
    # "scheme://" separator.
    source_path: Union[PurePath, str]
    destination_path: PurePath

    def __init__(
        self,
        *,
        source_path_format: PathFormat,
        source_path: Union[PurePath, str],
        destination_path: PurePath,
    ):
        if source_path_format == PathFormat.POSIX:
            if not isinstance(source_path, PurePosixPath):
                raise ValueError(
                    "Path mapping rule source_path_format does not match source_path type"
                )
        elif source_path_format == PathFormat.URI:
            if not isinstance(source_path, str):
                raise ValueError(
                    "Path mapping rule source_path must be a string for the URI source_path_format"
                )
            # Validate the shape here, where the offending value can be named.
            # The EXPR engine requires a real "scheme://" for a URI rule, and
            # these rules also reach it via the session's host context — without
            # this check a single-slash typo in a pathmapping-1.0 document
            # surfaces as an opaque ValueError out of Session() instead.
            if _URI_SOURCE_RE.match(source_path) is None:
                raise ValueError(
                    "Path mapping rule source_path for the URI source_path_format must "
                    f"begin with '<scheme>://', got {source_path!r}"
                )
        else:
            if not isinstance(source_path, PureWindowsPath):
                raise ValueError(
                    "Path mapping rule source_path_format does not match source_path type"
                )

        # This roundabout way can set the attributes of a frozen dataclass
        object.__setattr__(self, "source_path_format", source_path_format)
        object.__setattr__(self, "source_path", source_path)
        object.__setattr__(self, "destination_path", destination_path)

    @staticmethod
    def from_dict(rule: dict[str, str]) -> "PathMappingRule":
        """Builds a PathMappingRule from a dictionary representation
        with strings as values."""
        if not rule:
            raise ValueError("Empty path mapping rule")

        field_names = [field.name for field in fields(PathMappingRule)]
        for name in field_names:
            if name not in rule:
                raise ValueError(f"Path mapping rule requires the following fields: {field_names}")

        source_path_format = PathFormat(rule["source_path_format"].upper())
        source_path: Union[PurePath, str]
        if source_path_format == PathFormat.POSIX:
            source_path = PurePosixPath(rule["source_path"])
        elif source_path_format == PathFormat.URI:
            # Keep URIs verbatim; PurePath would collapse "scheme://".
            source_path = rule["source_path"]
        else:
            source_path = PureWindowsPath(rule["source_path"])
        destination_path = PurePath(rule["destination_path"])

        unsupported_fields = set(rule.keys()) - set(field_names)
        if unsupported_fields:
            raise ValueError(
                f"Unsupported fields for constructing path mapping rule: {unsupported_fields}"
            )

        return PathMappingRule(
            source_path_format=source_path_format,
            source_path=source_path,
            destination_path=destination_path,
        )

    def to_dict(self) -> dict[str, str]:
        """Returns a dictionary representation of the PathMappingRule."""
        return {
            "source_path_format": self.source_path_format.name,
            "source_path": str(self.source_path),
            "destination_path": str(self.destination_path),
        }

    @staticmethod
    def _uri_path_start(uri: str) -> Optional[int]:
        """Byte offset where the path component begins in a URI (after
        ``scheme://authority``), or None when there is no ``://`` separator.
        Mirrors openjd-rs's ``uri_path_start``."""
        scheme_sep = uri.find("://")
        if scheme_sep == -1:
            return None
        authority_start = scheme_sep + 3
        slash = uri.find("/", authority_start)
        return len(uri) if slash == -1 else slash

    def _apply_uri(self, path: str) -> tuple[bool, str]:
        """Apply a URI-format rule, mirroring openjd-rs's ``apply_uri``.

        Per RFC 3986 the scheme and authority match case-insensitively while
        the path portion matches case-sensitively, on whole path components.
        The result is a local path in the host's format.
        """
        sep = "/" if os_name == "posix" else "\\"
        source = str(self.source_path)
        src_path_start = self._uri_path_start(source)
        src_path_start = 0 if src_path_start is None else src_path_start
        inp_path_start = self._uri_path_start(path)
        inp_path_start = 0 if inp_path_start is None else inp_path_start

        # Scheme+authority must match case-insensitively, over ASCII only —
        # see _ASCII_LOWER_TABLE for why this is not str.lower().
        if _ascii_lower(path[:inp_path_start]) != _ascii_lower(source[:src_path_start]):
            return False, path
        # Path portion must match case-sensitively, on a component boundary.
        src_path = source[src_path_start:]
        inp_path = path[inp_path_start:]
        if not inp_path.startswith(src_path):
            return False, path
        remainder = inp_path[len(src_path) :]
        if remainder and not remainder.startswith("/"):
            return False, path

        child_parts = remainder[1:].split("/") if remainder else []
        result = str(self.destination_path)
        for part in child_parts:
            result += sep + part
        if path.endswith("/") and not result.endswith(sep):
            result += sep
        return True, result

    def _source_path_component_count(self) -> int:
        """Number of components in the source path, used to order rules from
        most to least specific. For URI sources the ``scheme://authority``
        counts as one component plus one per path segment."""
        if isinstance(self.source_path, PurePath):
            return len(self.source_path.parts)
        source = str(self.source_path)
        path_start = self._uri_path_start(source)
        if path_start is None:
            return 1
        path_portion = source[path_start:].strip("/")
        return 1 + (len(path_portion.split("/")) if path_portion else 0)

    def apply(self, *, path: str) -> tuple[bool, str]:
        """Applies the path mapping rule on the given path, if it matches the rule.
        Does not collapse ".." since symbolic paths could be used.

        Returns: tuple[bool, str] - indicating if the path matched the rule and the resulting
        mapped path. If it doesn't match, then it returns the original path unmodified.
        """
        if self.source_path_format == PathFormat.URI:
            return self._apply_uri(path)
        source_path = self.source_path
        # Unreachable: the constructor guarantees non-URI rules carry PurePath
        # sources. An assert rather than a raise because its only job here is to
        # narrow the Union for mypy.
        assert isinstance(source_path, PurePath)
        pure_path: PurePath
        if self.source_path_format == PathFormat.POSIX:
            pure_path = PurePosixPath(path)
            if not pure_path.is_relative_to(source_path):
                return False, path
        else:
            pure_path = PureWindowsPath(path)
            # Windows paths match case-insensitively, but over ASCII only —
            # PureWindowsPath.is_relative_to() folds with str.lower(), which also
            # folds non-ASCII characters (U+212A KELVIN SIGN lowers to ASCII
            # 'k'), so a homoglyph in a submitted path would remap here while the
            # EXPR engine's apply_path_mapping() and openjd-rs, which both use an
            # ASCII-only fold, leave it alone. RFC 0006 2.3.2 says the two are
            # the same transformation, so compare components ourselves.
            source_parts = source_path.parts
            path_parts = pure_path.parts
            if len(path_parts) < len(source_parts) or any(
                _ascii_lower(sp) != _ascii_lower(pp) for sp, pp in zip(source_parts, path_parts)
            ):
                return False, path

        remapped_parts = self.destination_path.parts + pure_path.parts[len(source_path.parts) :]
        if os_name == "posix":
            result = str(PurePosixPath(*remapped_parts))
            if self._has_trailing_slash(self.source_path_format, path):
                result += "/"
        else:
            result = str(PureWindowsPath(*remapped_parts))
            if self._has_trailing_slash(self.source_path_format, path):
                result += "\\"

        return True, result

    def _has_trailing_slash(self, os: PathFormat, path: str) -> bool:
        if os == PathFormat.POSIX:
            return path.endswith("/")
        # A Windows path may use either separator, and the EXPR engine (and
        # openjd-rs) accept both here.
        return path.endswith("\\") or path.endswith("/")
