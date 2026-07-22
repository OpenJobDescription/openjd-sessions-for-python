# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass, fields
from enum import Enum
from os import name as os_name
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Optional, Union


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

        # Scheme+authority must match case-insensitively.
        if path[:inp_path_start].lower() != source[:src_path_start].lower():
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

    def source_path_component_count(self) -> int:
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
        if not isinstance(source_path, PurePath):
            # The constructor guarantees non-URI rules carry PurePath sources.
            raise TypeError(
                "Path mapping rule source_path must be a PurePath for filesystem source formats"
            )
        pure_path: PurePath
        if self.source_path_format == PathFormat.POSIX:
            pure_path = PurePosixPath(path)
        else:
            pure_path = PureWindowsPath(path)

        if not pure_path.is_relative_to(source_path):
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
        else:
            return path.endswith("\\")
