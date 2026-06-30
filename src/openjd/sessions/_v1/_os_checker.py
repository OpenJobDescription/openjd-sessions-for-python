# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import os
import sys

LINUX = "linux"
MACOS = "darwin"
POSIX = "posix"
WINDOWS = "nt"


def is_linux() -> bool:
    return sys.platform == LINUX


def is_posix() -> bool:
    return os.name == POSIX


def is_windows() -> bool:
    return os.name == WINDOWS


def check_os() -> str:
    return "win32" if is_windows() else "posix"
