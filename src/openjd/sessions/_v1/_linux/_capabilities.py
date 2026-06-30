# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""This module contains code for interacting with Linux capabilities. The module uses the ctypes
module from the Python standard library to wrap the libcap library.

See https://man7.org/linux/man-pages/man7/capabilities.7.html for details on this Linux kernel
feature.
"""

import ctypes
import os
import sys
from contextlib import contextmanager
from ctypes.util import find_library
from enum import Enum
from functools import cache
from typing import Any, Generator, Optional, Tuple, TYPE_CHECKING


from .._logging import LOG


# Capability sets
CAP_EFFECTIVE = 0
CAP_PERMITTED = 1
CAP_INHERITABLE = 2

# Capability bit numbers
CAP_KILL = 5

# Values for cap_flag_value_t arguments
CAP_CLEAR = 0
CAP_SET = 1

cap_flag_t = ctypes.c_int
cap_flag_value_t = ctypes.c_int
cap_value_t = ctypes.c_int


class CapabilitySetType(Enum):
    INHERITABLE = CAP_INHERITABLE
    PERMITTED = CAP_PERMITTED
    EFFECTIVE = CAP_EFFECTIVE


class UserCapHeader(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("pid", ctypes.c_int),
    ]


class UserCapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


class Cap(ctypes.Structure):
    _fields_ = [
        ("head", UserCapHeader),
        ("data", UserCapData),
    ]


if TYPE_CHECKING:
    cap_t = ctypes._Pointer[Cap]
    cap_flag_value_ptr = ctypes._Pointer[cap_flag_value_t]
    cap_value_ptr = ctypes._Pointer[cap_value_t]
    ssize_ptr_t = ctypes._Pointer[ctypes.c_ssize_t]
else:
    cap_t = ctypes.POINTER(Cap)
    cap_flag_value_ptr = ctypes.POINTER(cap_flag_value_t)
    cap_value_ptr = ctypes.POINTER(cap_value_t)
    ssize_ptr_t = ctypes.POINTER(ctypes.c_ssize_t)


def _cap_set_err_check(
    result: ctypes.c_int,
    func: Any,
    args: Tuple[Any, ...],
) -> ctypes.c_int:
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def _cap_get_proc_err_check(
    result: cap_t,
    func: Any,
    args: Tuple[cap_t, cap_flag_t, ctypes.c_int, cap_value_ptr, cap_flag_value_t],
) -> cap_t:
    if not result:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def _cap_get_flag_errcheck(
    result: ctypes.c_int, func: Any, args: Tuple[cap_t, cap_value_t, cap_flag_t, cap_flag_value_ptr]
) -> ctypes.c_int:
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


@cache
def _get_libcap() -> Optional[ctypes.CDLL]:
    if not sys.platform.startswith("linux"):
        raise OSError(f"libcap is only available on Linux, but found platform: {sys.platform}")

    libcap_path = find_library("cap")
    if libcap_path is None:
        LOG.info(
            "Unable to locate libcap. Session action cancelation signals will be sent using sudo"
        )
        return None

    libcap = ctypes.CDLL(libcap_path, use_errno=True)

    # https://man7.org/linux/man-pages/man3/cap_set_proc.3.html
    libcap.cap_set_proc.restype = ctypes.c_int
    libcap.cap_set_proc.argtypes = [
        cap_t,
    ]
    libcap.cap_set_proc.errcheck = _cap_set_err_check  # type: ignore

    # https://man7.org/linux/man-pages/man3/cap_get_proc.3.html
    libcap.cap_get_proc.restype = cap_t
    libcap.cap_get_proc.argtypes = []
    libcap.cap_get_proc.errcheck = _cap_get_proc_err_check  # type: ignore

    # https://man7.org/linux/man-pages/man3/cap_set_flag.3.html
    libcap.cap_set_flag.restype = ctypes.c_int
    libcap.cap_set_flag.argtypes = [
        cap_t,
        cap_flag_t,
        ctypes.c_int,
        cap_value_ptr,
        cap_flag_value_t,
    ]

    # https://man7.org/linux/man-pages/man3/cap_get_flag.3.html
    libcap.cap_get_flag.restype = ctypes.c_int
    libcap.cap_get_flag.argtypes = (
        cap_t,
        cap_value_t,
        cap_flag_t,
        cap_flag_value_ptr,
    )
    libcap.cap_get_flag.errcheck = _cap_get_flag_errcheck  # type: ignore

    return libcap


def _has_capability(
    *,
    libcap: ctypes.CDLL,
    caps: cap_t,
    capability: int,
    capability_set_type: CapabilitySetType,
) -> bool:
    flag_value = cap_flag_value_t()
    libcap.cap_get_flag(caps, capability, capability_set_type.value, ctypes.byref(flag_value))
    return flag_value.value == CAP_SET


@contextmanager
def try_use_cap_kill() -> Generator[bool, None, None]:
    """
    A context-manager that attempts to leverage the CAP_KILL Linux capability.

    If CAP_KILL is in the current thread's effective set, this context-manager takes no action and
    yields True.

    If CAP_KILL is not in the effective set but is in the permitted set, the context-manager:
        1.  adds CAP_KILL to the effective set before entering the context-manager
        2.  yields True
        3.  clears CAP_KILL from the effective set when exiting the context-manager

    Otherwise, the context-manager does nothing and yields False

    Returns:
        A context manager that yields a bool. See above for details.
    """
    if not sys.platform.startswith("linux"):
        raise OSError(f"Only Linux is supported, but platform is {sys.platform}")

    libcap = _get_libcap()
    # If libcap is not found, we yield False indicating we are not aware of having CAP_KILL
    if not libcap:
        yield False
        return

    caps = libcap.cap_get_proc()

    if _has_capability(
        libcap=libcap,
        caps=caps,
        capability=CAP_KILL,
        capability_set_type=CapabilitySetType.EFFECTIVE,
    ):
        LOG.debug("CAP_KILL is in the thread's effective set")
        # CAP_KILL is already in the effective set
        yield True
    elif _has_capability(
        libcap=libcap,
        caps=caps,
        capability=CAP_KILL,
        capability_set_type=CapabilitySetType.PERMITTED,
    ):
        # CAP_KILL is in the permitted set. We will temporarily add it to the effective set
        LOG.debug("CAP_KILL is in the thread's permitted set. Temporarily adding to effective set")
        cap_value_arr_t = cap_value_t * 1
        cap_value_arr = cap_value_arr_t()
        cap_value_arr[0] = CAP_KILL
        libcap.cap_set_flag(
            caps,
            CAP_EFFECTIVE,
            1,
            cap_value_arr,
            CAP_SET,
        )
        libcap.cap_set_proc(caps)
        try:
            yield True
        finally:
            # Clear CAP_KILL from the effective set
            LOG.debug("Clearing CAP_KILL from the thread's effective set")
            libcap.cap_set_flag(
                caps,
                CAP_EFFECTIVE,
                1,
                cap_value_arr,
                CAP_CLEAR,
            )
            libcap.cap_set_proc(caps)
    else:
        yield False


def main() -> None:
    """A developer debugging entrypoint for testing the try_use_cap_kill() behaviour"""
    import logging

    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("openjd.sessions").setLevel(logging.DEBUG)

    with try_use_cap_kill() as has_cap_kill:
        LOG.info("Has CAP_KILL: %s", has_cap_kill)


if __name__ == "__main__":
    main()
