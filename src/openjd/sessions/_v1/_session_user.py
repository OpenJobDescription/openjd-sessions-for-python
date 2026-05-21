# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Session user types — direct re-exports from the Rust extension.

`PosixSessionUser`, `WindowsSessionUser`, and `BadCredentialsException` are
implemented entirely in Rust (in the `openjd._openjd_rs` extension built from
the openjd-model-for-python crate). This module re-exports them under their
canonical public location.

`WindowsSessionUser` validates credentials unconditionally on construction
via `LogonUserW`. There is no Python-level override hook (the legacy
`_validate_username_password` static method is gone). Test harnesses that
need to construct fake `WindowsSessionUser` instances must mock at a
different layer — e.g. by patching the binding constructor itself, or by
having their fixtures return a `MagicMock` spec'd against the class.
"""

from typing import Union

from openjd._openjd_rs import (
    PosixSessionUser,
    WindowsSessionUser,
    BadCredentialsException,
)

__all__ = (
    "PosixSessionUser",
    "SessionUser",
    "WindowsSessionUser",
    "BadCredentialsException",
)


# Type alias for callers that accept either user kind. Mirrors the legacy
# `SessionUser` re-export.
SessionUser = Union[PosixSessionUser, WindowsSessionUser]
