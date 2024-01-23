# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import os
import random
import string
from logging import INFO, LoggerAdapter, getLogger
from logging.handlers import QueueHandler
from queue import Empty, SimpleQueue
from typing import Generator

import pytest

from openjd.sessions import PosixSessionUser
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._windows_permission_helper import WindowsPermissionHelper

if is_windows():
    import win32net
    import win32netcon


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


@pytest.fixture(scope="function")
def message_queue() -> SimpleQueue:
    return SimpleQueue()


@pytest.fixture(scope="function")
def queue_handler(message_queue: SimpleQueue) -> QueueHandler:
    return QueueHandler(message_queue)


@pytest.fixture(scope="function")
def session_id() -> str:
    return "some Id"


@pytest.fixture(scope="function")
def working_directory(win_test_user) -> str:
    username, _ = win_test_user
    process_user = WindowsPermissionHelper.get_process_user()

    working_dir = os.path.join(os.getcwd(), "working_dir")
    try:
        os.removedirs(working_dir)
    except:
        pass
    os.mkdir(working_dir)
    WindowsPermissionHelper.set_permissions_full_control(working_dir, [username, process_user])
    yield working_dir
    os.remove(working_dir)


@pytest.fixture(scope="session")
def win_test_user() -> Generator:
    def generate_strong_password() -> str:
        password_length = 14

        # Generate at least one character from each category
        uppercase = random.choice(string.ascii_uppercase)
        lowercase = random.choice(string.ascii_lowercase)
        digit = random.choice(string.digits)

        # Ensure the rest of the password is made up of a random mix of characters
        remaining_length = password_length - 4
        other_chars = "".join(
            random.choice(string.ascii_letters + string.digits + string.punctuation)
            for _ in range(remaining_length)
        )

        # Combine and shuffle
        password_characters = list(uppercase + lowercase + digit + other_chars)
        random.shuffle(password_characters)
        return "".join(password_characters)

    username = "OpenJDTester"
    # No one need to know this password. So we will generate it randomly.
    password = generate_strong_password()

    def create_user() -> None:
        try:
            win32net.NetUserGetInfo(None, username, 1)
            print(f"User '{username}' already exists. Skip the User Creation")
        except win32net.error:
            # https://learn.microsoft.com/en-us/windows/win32/api/lmaccess/nf-lmaccess-netuseradd#examples
            user_info = {
                "name": username,
                "password": password,
                # The privilege level of the user. USER_PRIV_USER is a standard user.
                "priv": win32netcon.USER_PRIV_USER,
                "home_dir": None,
                "comment": None,
                # Account control flags. UF_SCRIPT is required here.
                "flags": win32netcon.UF_SCRIPT,
                "script_path": None,
            }
            try:
                win32net.NetUserAdd(None, 1, user_info)
                print(f"User '{username}' created successfully.")
            except Exception as e:
                print(f"Failed to create user '{username}': {e}")
                raise e

    def add_user_to_group(group_name: str):
        try:
            win32net.NetLocalGroupAddMembers(None, group_name, 3, [{"domainandname": username}])
            print(f"User {username} added to local users group successfully.")
        except Exception as e:
            print(f"Failed to add user {username} to local users group ")
            raise e

    def delete_user() -> None:
        try:
            win32net.NetUserDel(None, username)
            print(f"User '{username}' deleted successfully.")
        except win32net.error as e:
            print(f"Failed to delete user '{username}': {e}")
            raise e

    create_user()
    add_user_to_group("USERS")
    yield username, password
    # Delete the user after test completes
    delete_user()
