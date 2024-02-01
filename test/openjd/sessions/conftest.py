# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import random
import string
from logging import INFO, LoggerAdapter, getLogger
from logging.handlers import QueueHandler
from queue import Empty, SimpleQueue

import pytest

from openjd.sessions import PosixSessionUser, WindowsSessionUser, BadCredentialsException
from openjd.sessions._os_checker import is_posix, is_windows

WIN_USERNAME_ENV_VAR = "OJD_SESSIONS_USER_NAME"
WIN_PASS_ENV_VAR = "OJD_SESSIONS_USER_PASSWORD"


def build_logger(handler: QueueHandler) -> LoggerAdapter:
    charset = string.ascii_letters + string.digits + string.punctuation
    name_suffix = "".join(random.choices(charset, k=32))
    log = getLogger(".".join((__name__, name_suffix)))
    log.setLevel(INFO)
    log.addHandler(handler)
    return LoggerAdapter(log, extra=dict())


def collect_queue_messages(queue: SimpleQueue) -> list[str]:
    """Extract the text of messages from a SimpleQueue containing LogRecords"""
    messages: list[str] = []
    try:
        while True:
            messages.append(queue.get_nowait().getMessage())
    except Empty:
        pass
    return messages


def has_posix_target_user() -> bool:
    """Has the testing environment exported the env variables for doing
    cross-account posix target-user tests.
    These are tests where the target user has a group in common with
    this process' user.
    """
    return (
        os.environ.get("OPENJD_TEST_SUDO_TARGET_USER") is not None
        and os.environ.get("OPENJD_TEST_SUDO_SHARED_GROUP") is not None
    )


def has_posix_disjoint_user() -> bool:
    """Has the testing environment exported the env variables for doing
    cross-account posix disjoint-user tests.
    These are tests where the disjoint user has NO group in common with
    this process' user.
    """
    return (
        os.environ.get("OPENJD_TEST_SUDO_DISJOINT_USER") is not None
        and os.environ.get("OPENJD_TEST_SUDO_DISJOINT_GROUP") is not None
    )


@pytest.fixture(scope="function")
def posix_target_user() -> PosixSessionUser:
    if not is_posix():
        pytest.skip("Posix-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ["OPENJD_TEST_SUDO_TARGET_USER"]
    return PosixSessionUser(
        user=user,
        # Intentionally fail if the var is not defined.
        group=os.environ["OPENJD_TEST_SUDO_SHARED_GROUP"],
    )


@pytest.fixture(scope="function")
def posix_disjoint_user() -> PosixSessionUser:
    if not is_posix():
        pytest.skip("Posix-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ["OPENJD_TEST_SUDO_DISJOINT_USER"]
    return PosixSessionUser(
        user=user,
        # Intentionally fail if the var is not defined.
        group=os.environ["OPENJD_TEST_SUDO_DISJOINT_GROUP"],
    )


def has_windows_user() -> bool:
    """Has the testing environment exported the env variables for doing
    cross-account Windows tests.
    """
    return (
        os.environ.get(WIN_USERNAME_ENV_VAR) is not None
        and os.environ.get(WIN_PASS_ENV_VAR) is not None
    )


@pytest.fixture(scope="function")
def windows_user() -> WindowsSessionUser:
    if not is_windows():
        pytest.skip("Windows-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ[WIN_USERNAME_ENV_VAR]
    password = os.environ[WIN_PASS_ENV_VAR]

    try:
        return WindowsSessionUser(user, password=password)
    except BadCredentialsException as e:
        raise Exception("Invalid credentials for cross-user test account.") from e


@pytest.fixture(scope="function")
def message_queue() -> SimpleQueue:
    return SimpleQueue()


@pytest.fixture(scope="function")
def queue_handler(message_queue: SimpleQueue) -> QueueHandler:
    return QueueHandler(message_queue)


@pytest.fixture(scope="function")
def session_id() -> str:
    return "some Id"
