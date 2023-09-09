# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass, fields
from enum import Enum
from os import name as os_name
from pathlib import PurePath, PurePosixPath, PureWindowsPath


class PathFormat(str, Enum):
    POSIX = "POSIX"
    WINDOWS = "WINDOWS"


@dataclass(frozen=True)
class PathMappingRule:
    source_path_format: PathFormat
    source_path: PurePath
    destination_path: PurePath

    def __init__(
        self, *, source_path_format: PathFormat, source_path: PurePath, destination_path: PurePath
    ):
        if source_path_format == PathFormat.POSIX:
            if not isinstance(source_path, PurePosixPath):
                raise ValueError(
                    "Path mapping rule source_path_format does not match source_path type"
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
        source_path: PurePath
        if source_path_format == PathFormat.POSIX:
            source_path = PurePosixPath(rule["source_path"])
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

    def apply(self, *, path: str) -> tuple[bool, str]:
        """Applies the path mapping rule on the given path, if it matches the rule.
        Does not collapse ".." since symbolic paths could be used.

        Returns: tuple[bool, str] - indicating if the path matched the rule and the resulting
        mapped path. If it doesn't match, then it returns the original path unmodified.
        """
        pure_path: PurePath
        if self.source_path_format == PathFormat.POSIX:
            pure_path = PurePosixPath(path)
        else:
            pure_path = PureWindowsPath(path)

        if not pure_path.is_relative_to(self.source_path):
            return False, path

        remapped_parts = (
            self.destination_path.parts + pure_path.parts[len(self.source_path.parts) :]
        )
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
