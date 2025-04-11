# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import random
import string
from logging import INFO, getLogger
from logging.handlers import QueueHandler
from queue import Empty, SimpleQueue
from typing import Generator, Optional
from hashlib import sha256
from unittest.mock import MagicMock
import pytest

from openjd.sessions import PosixSessionUser, WindowsSessionUser, BadCredentialsException
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._logging import LoggerAdapter
from openjd.sessions._action_filter import ActionMonitoringFilter
from openjd.model import RevisionExtensions, SpecificationRevision

if is_windows():
    from openjd.sessions._win32._helpers import (  # type: ignore
        get_current_process_session_id,
        logon_user_context,
    )

    TEST_RUNNING_IN_WINDOWS_SESSION_0 = 0 == get_current_process_session_id()
else:
    TEST_RUNNING_IN_WINDOWS_SESSION_0 = False

WIN_USERNAME_ENV_VAR = "OPENJD_TEST_WIN_USER_NAME"
WIN_PASS_ENV_VAR = "OPENJD_TEST_WIN_USER_PASSWORD"
WIN_SET_TEST_ENV_VARS_MESSAGE = f"Must define environment vars {WIN_USERNAME_ENV_VAR} and {WIN_PASS_ENV_VAR} to run impersonation tests on Windows."

POSIX_TARGET_USER_ENV_VAR = "OPENJD_TEST_SUDO_TARGET_USER"
POSIX_SHARED_GROUP_ENV_VAR = "OPENJD_TEST_SUDO_SHARED_GROUP"
POSIX_DISJOINT_USER_ENV_VAR = "OPENJD_TEST_SUDO_DISJOINT_USER"
POSIX_DISJOINT_GROUP_ENV_VAR = "OPENJD_TEST_SUDO_DISJOINT_GROUP"

POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE = f"Must define environment vars {POSIX_TARGET_USER_ENV_VAR} and {POSIX_SHARED_GROUP_ENV_VAR} to run target-user impersonation tests on posix."
POSIX_SET_DISJOINT_USER_ENV_VARS_MESSAGE = f"Must define environment vars {POSIX_DISJOINT_USER_ENV_VAR} and {POSIX_DISJOINT_GROUP_ENV_VAR} to run target-user impersonation tests on posix."


def pytest_collection_modifyitems(config, items):
    """This is a pytest hook that provides a default mark expression if one was not provided. By
    default, we want to de-select tests that require the CAP_KILL Linux capability.

    Those tests should only be selected when running the Docker container test workflow
    described in DEVELOPMENT.md which grant the necessary capabilities and specify a
    mark expression.

    See:
    - https://docs.pytest.org/en/8.3.x/reference/reference.html#pytest.hookspec.pytest_collection_modifyitems
    - https://docs.pytest.org/en/8.3.x/reference/reference.html#command-line-flags
    """
    mark_expr = config.getoption("markexpr", False)
    if not mark_expr:
        config.option.markexpr = "not requires_cap_kill"
    else:
        config.option.markexpr = mark_expr


def create_unique_logger_name(prefix: str = "", seed: Optional[str] = None) -> str:
    """Create a unique logger name using a hash to avoid collisions.

    Args:
        prefix: Optional prefix for the logger name
        seed: Optional seed string to use for generating the hash

    Returns:
        A unique logger name
    """
    if seed:
        h = sha256()
        h.update(seed.encode("utf-8"))
        suffix = h.hexdigest()[0:32]
    else:
        charset = string.ascii_letters + string.digits
        suffix = "".join(random.choices(charset, k=32))

    return f"{prefix}{suffix}"


def build_logger(handler: QueueHandler) -> LoggerAdapter:
    """Build a logger for testing purposes.

    Args:
        handler: The queue handler to attach to the logger

    Returns:
        A configured LoggerAdapter
    """
    name_suffix = create_unique_logger_name()
    log = getLogger(".".join((__name__, name_suffix)))
    log.setLevel(INFO)
    log.addHandler(handler)
    return LoggerAdapter(log, extra=dict())


def setup_action_filter_test(
    queue_handler: QueueHandler,
    session_id: str = "foo",
    callback: Optional[MagicMock] = None,
    suppress_filtered: bool = False,
    enabled_extensions: Optional[list[str]] = None,
) -> tuple[LoggerAdapter, ActionMonitoringFilter, MagicMock]:
    """Set up a test environment for testing ActionMonitoringFilter.

    This helper method creates a unique logger name, sets up the ActionMonitoringFilter,
    and configures the logger with the filter.

    Args:
        queue_handler: The QueueHandler to attach to the logger
        session_id: The session ID to use for the filter
        callback: Optional mock callback to use for the filter
        suppress_filtered: Whether to suppress filtered messages
        enabled_extensions: Optional list of extensions to enable

    Returns:
        A tuple containing (logger_adapter, action_filter, callback_mock)

    Note:
        This helper works for most tests, but for tests that need to verify specific
        callback behavior with redacted values, it's better to create the filter and
        logger directly in the test. This is because when multiple filters are applied
        to the same log message (which can happen when running multiple tests), the
        redaction can happen before the callback is invoked, resulting in the callback
        receiving redacted values instead of the original values.
    """
    # Create a unique logger name WITHOUT using the message as seed
    # This ensures each test gets a truly unique logger name
    logger_name = create_unique_logger_name(prefix="action_filter_")

    # Create a mock callback if one wasn't provided
    if callback is None:
        callback = MagicMock()

    # Create a RevisionExtensions with the provided extensions or an empty set
    revision_extensions = RevisionExtensions(
        spec_rev=SpecificationRevision.v2023_09, supported_extensions=enabled_extensions or []
    )

    # Create the filter directly with the provided parameters
    action_filter = ActionMonitoringFilter(
        session_id=session_id,
        callback=callback,
        suppress_filtered=suppress_filtered,
        revision_extensions=revision_extensions,
    )

    # Set up the logger
    log = getLogger(".".join((__name__, logger_name)))
    log.setLevel(INFO)
    log.addHandler(queue_handler)
    log.addFilter(action_filter)

    # Create and return the logger adapter with the session_id
    # This is critical for the filter to work properly
    logger_adapter = LoggerAdapter(log, extra={"session_id": session_id})

    return logger_adapter, action_filter, callback


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
        os.environ.get(POSIX_TARGET_USER_ENV_VAR) is not None
        and os.environ.get(POSIX_SHARED_GROUP_ENV_VAR) is not None
    )


def has_posix_disjoint_user() -> bool:
    """Has the testing environment exported the env variables for doing
    cross-account posix disjoint-user tests.
    These are tests where the disjoint user has NO group in common with
    this process' user.
    """
    return (
        os.environ.get(POSIX_DISJOINT_USER_ENV_VAR) is not None
        and os.environ.get(POSIX_DISJOINT_GROUP_ENV_VAR) is not None
    )


@pytest.fixture(scope="function")
def posix_target_user() -> PosixSessionUser:
    if not is_posix():
        pytest.skip("Posix-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ.get(POSIX_TARGET_USER_ENV_VAR)
    group = os.environ.get(POSIX_SHARED_GROUP_ENV_VAR)
    if user is None or group is None:
        pytest.xfail(POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE)
    return PosixSessionUser(
        user=user,
        group=group,
    )


@pytest.fixture(scope="function")
def posix_disjoint_user() -> PosixSessionUser:
    if not is_posix():
        pytest.skip("Posix-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ.get(POSIX_DISJOINT_USER_ENV_VAR)
    group = os.environ.get(POSIX_DISJOINT_GROUP_ENV_VAR)
    if user is None or group is None:
        pytest.xfail(POSIX_SET_DISJOINT_USER_ENV_VARS_MESSAGE)
    return PosixSessionUser(
        user=user,
        group=group,
    )


def has_windows_user() -> bool:
    """Has the testing environment exported the env variables for doing
    cross-account Windows tests.
    """
    return (
        os.environ.get(WIN_USERNAME_ENV_VAR) is not None
        and os.environ.get(WIN_PASS_ENV_VAR) is not None
    )


def are_tests_in_windows_session_0() -> bool:
    return TEST_RUNNING_IN_WINDOWS_SESSION_0


@pytest.fixture(scope="session")
def windows_user() -> Generator[WindowsSessionUser, None, None]:
    if not is_windows():
        pytest.skip("Windows-specific feature")
    # Intentionally fail if the var is not defined.
    user = os.environ.get(WIN_USERNAME_ENV_VAR)
    password = os.environ.get(WIN_PASS_ENV_VAR)
    if user is None or password is None:
        pytest.xfail(WIN_SET_TEST_ENV_VARS_MESSAGE)

    if TEST_RUNNING_IN_WINDOWS_SESSION_0:
        try:
            # Note: We don't load the user profile; it's currently not needed by our tests,
            # and we're getting a mysterious crash when unloading it.
            with logon_user_context(user, password) as logon_token:
                yield WindowsSessionUser(user, logon_token=logon_token)
        except OSError as e:
            raise Exception(
                f"Could not logon as {user}. Check the password that was provided in {WIN_PASS_ENV_VAR}."
            ) from e
    else:
        # Use the username + password to create subprocesses
        try:
            yield WindowsSessionUser(user, password=password)
        except BadCredentialsException as e:
            raise Exception(
                f"Could not logon as {user}. Check the password that was provided in {WIN_PASS_ENV_VAR}."
            ) from e


@pytest.fixture(scope="function")
def message_queue() -> SimpleQueue:
    return SimpleQueue()


@pytest.fixture(scope="function")
def queue_handler(message_queue: SimpleQueue) -> QueueHandler:
    return QueueHandler(message_queue)


@pytest.fixture(scope="function")
def session_id() -> str:
    return "some Id"
