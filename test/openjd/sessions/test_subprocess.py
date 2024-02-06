# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for LoggingSubprocess"""
import shutil
import sys
import tempfile
import time
import os
import getpass
from concurrent.futures import ThreadPoolExecutor, wait
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Union
from unittest.mock import MagicMock

import pytest

from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._session_user import PosixSessionUser, WindowsSessionUser
from openjd.sessions._subprocess import LoggingSubprocess

from .conftest import build_logger, collect_queue_messages, has_posix_target_user


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestLoggingSubprocessSameUser:
    """Tests of the LoggingSubprocess where the subprocess is being run as the same
    user as the owner of this process.
    """

    def test_must_have_args(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        with pytest.raises(ValueError):
            LoggingSubprocess(logger=logger, args=[])

    def test_getters_return_none(self, queue_handler: QueueHandler) -> None:
        # Check that the getters all return None if the subprocess hasn't run yet.

        # GIVEN
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", 'print("Test")'],
        )

        # THEN
        assert subproc.pid is None
        assert subproc.exit_code is None
        assert not subproc.is_running

    @pytest.mark.parametrize("exitcode", [0, 1])
    def test_basic_operation(
        self, exitcode: int, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Can we run a process, capture its output, and discover its return code?

        # GIVEN
        logger = build_logger(queue_handler)
        message = "this is 'output'"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", f'import sys; print("{message}"); sys.exit({exitcode})'],
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == exitcode
        assert not subproc.failed_to_start
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)
        assert message in messages

    @pytest.mark.parametrize("exitcode", [0, 1])
    def test_basic_operation_with_sameuser(
        self, exitcode: int, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Can we run a process, capture its output, and discover its return code?

        # GIVEN
        user: Union[PosixSessionUser, WindowsSessionUser]
        if is_posix():
            current_user = getpass.getuser()
            user = PosixSessionUser(user=current_user)
        else:
            current_user = WindowsSessionUser.get_process_user()
            user = WindowsSessionUser(user=current_user)

        logger = build_logger(queue_handler)
        message = "this is output"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", f'import sys; print("{message}"); sys.exit({exitcode})'],
            user=user,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == exitcode
        assert not subproc.failed_to_start
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)
        assert message in messages

    def test_cannot_run(self, message_queue: SimpleQueue, queue_handler: QueueHandler) -> None:
        # Make sure that we log a message, and don't blow up when we cannot
        # run the process for some reason.

        # GIVEN
        logger = build_logger(queue_handler)
        args = [tempfile.gettempdir()] if is_posix() else ["test_failed_command"]
        subproc = LoggingSubprocess(
            logger=logger,
            # The temp dir definitely isn't an executable application
            args=args,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        messages = collect_queue_messages(message_queue)
        assert subproc.pid is None
        assert subproc.exit_code is None
        assert subproc.failed_to_start
        assert any(message.startswith("Process failed to start") for message in messages)

    def test_cannot_run_with_callback(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Make sure that we call the callback, and don't blow up when we cannot
        # run the process for some reason.

        # GIVEN
        logger = build_logger(queue_handler)
        callback_mock = MagicMock()
        subproc = LoggingSubprocess(
            logger=logger,
            # The temp dir definitely isn't an executable application
            args=[tempfile.gettempdir()],
            callback=callback_mock,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        callback_mock.assert_called_once()

    def test_captures_stderr(self, message_queue: SimpleQueue, queue_handler: QueueHandler) -> None:
        # Ensure that messages sent to stderr are logged

        # GIVEN
        logger = build_logger(queue_handler)
        message = "this is output"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", f'import sys; print("{message}", file=sys.stderr)'],
        )

        # WHEN
        subproc.run()

        # THEN
        messages = collect_queue_messages(message_queue)
        assert message in messages

    def test_cannot_run_twice(self, queue_handler: QueueHandler) -> None:
        # We should fail if we try to run a LoggingSubprocess twice

        # GIVEN
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", "print('Test')"],
        )

        # WHEN
        subproc.run()

        # THEN
        with pytest.raises(RuntimeError):
            subproc.run()

    def test_invokes_callback(self, queue_handler: QueueHandler) -> None:
        # Make sure that the given callback is invoked when the process exits.

        # GIVEN
        logger = build_logger(queue_handler)
        callback_mock = MagicMock()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                sys.executable,
                "-c",
                "print('This is just a test')",
            ],
            callback=callback_mock,
        )

        # WHEN
        subproc.run()

        # THEN
        callback_mock.assert_called_once()

    def test_notify_ends_process(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Make sure that process is sent a notification signal

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
        )

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            # For Windows, allow additional time for Powershell
            time.sleep(1 if not is_windows() else 5)
            subproc.notify()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        messages = collect_queue_messages(message_queue)
        assert "Trapped" in messages
        # Check for the first message that would print
        assert "Log from test 0" in messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in messages
        assert subproc.exit_code != 0

    def test_terminate_ends_process(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler
    ) -> None:
        # Make sure that the subprocess is forcefully killed when terminated

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running

        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
        assert subproc.exit_code != 0

    @pytest.mark.xfail(
        os.environ.get("CODEBUILD_BUILD_ID", None) is not None,
        reason="This test is failing exclusively in codebuild; unblocking, and will root cause later.",
    )
    def test_terminate_ends_process_tree(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Make sure that the subprocess and all of its children are forcefully killed when terminated
        from psutil import Process, NoSuchProcess

        # GIVEN
        logger = build_logger(queue_handler)
        if is_posix():
            script_loc = (Path(__file__).parent / "support_files" / "app_20s_run.sh").resolve()
        else:
            script_loc = (Path(__file__).parent / "support_files" / "app_20s_run.ps1").resolve()

        args = [str(script_loc), sys.executable]
        if is_windows():
            args.insert(0, "powershell")
        subproc = LoggingSubprocess(
            logger=logger,
            args=args,
        )

        def end_proc():
            time.sleep(1)
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            subproc.wait_until_started()
            children = list[Process]()
            attempt = 0
            # For Windows, we will have 2 processes
            expected_num_children = 2 if is_windows() else 1
            # Then give the subprocess some time to finish loading and start running some children.
            while len(children) < expected_num_children and attempt < 50:
                time.sleep(0.25)
                children = Process(subproc.pid).children(recursive=True)
                attempt += 1
            # give process time to get running
            time.sleep(2)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        messages = collect_queue_messages(message_queue)
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in messages
        # Check for the first message that would print
        assert "Log from test 0" in messages
        # If there's no 19, then we ended before the app naturally finished.
        assert "Log from test 19" not in messages
        assert subproc.exit_code != 0
        assert len(children) == expected_num_children  # .sh/.ps1 script runs a .py script

        num_children_running = 0
        for _ in range(0, 50):
            time.sleep(0.25)  # Give the child process some time to end.
            num_children_running = 0
            for child in children:
                try:
                    # Raises NoSuchProcess if the process is gone
                    child.status()
                    num_children_running += 1
                except NoSuchProcess:
                    # Expected. This is a success
                    pass
            if num_children_running == 0:
                break
        assert num_children_running == 0

    def test_run_reads_max_line_length(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Make sure the run method reads up to a max line length

        # GIVEN
        expected_max_line_length = 64 * 1000
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                sys.executable,
                "-c",
                f"""import sys
print("a" * {expected_max_line_length}, end="")
print("b" * {expected_max_line_length}, end="")
print("c")
sys.exit(0)
""",
            ],
        )

        # WHEN
        subproc.run()

        # THEN
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)

        expected_messages = [
            "a" * expected_max_line_length,
            "b" * expected_max_line_length,
            "c",
        ]
        assert list_has_items_in_order(expected_messages, messages)
        all(len(m) <= expected_max_line_length for m in messages)


def list_has_items_in_order(expected: list, actual: list) -> bool:
    """
    Checks whether the items in list `expected` appear in the same order in the list `actual`,
    allowing any number of elements between them.

    Args:
        expected (list): List of items expected to appear in the same order in `actual`
        actual (list): List of items to check for from `expected`

    Returns:
        bool: Whether the `expected` items appeared in order in `actual`
    """
    e = 0
    a = 0
    while e < len(expected) and a < len(actual):
        if expected[e] == actual[a]:
            e += 1
        a += 1
    return e == len(expected)


@pytest.mark.xfail(
    not has_posix_target_user(),
    reason="Must be running inside of the sudo_environment testing container.",
)
@pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
class TestLoggingSubprocessPosix(object):
    """Tests for LoggingSubprocess's ability to run the subprocess as a separate user
    on POSIX systems using sudo."""

    @pytest.mark.parametrize("exitcode", [0, 1])
    def test_basic_operation(
        self,
        exitcode: int,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        posix_target_user: PosixSessionUser,
    ) -> None:
        # Test that we run the subprocess as a desired user that differs from the current user.

        # GIVEN
        logger = build_logger(queue_handler)
        message = "this is output"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                # Note: Intentionally not `sys.executable`. Reasons:
                #  1) This is a cross-account command, and sys.executable may be in a user-specific venv
                #  2) This test is, generally, intended to be run in a docker container where the system
                #     python is the correct version that we want to run under.
                "python",
                "-c",
                f'import sys; import getpass; print(getpass.getuser()); print("{message}"); sys.exit({exitcode})',
            ],
            user=posix_target_user,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == exitcode
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)
        assert message in messages
        assert posix_target_user.user in messages

    @pytest.mark.usefixtures("posix_target_user")
    def test_notify_ends_process(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        posix_target_user: PosixSessionUser,
    ) -> None:
        # Make sure that process is sent a notification signal

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        shutil.chown(python_app_loc, group=posix_target_user.group)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
            user=posix_target_user,
        )

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            time.sleep(1)
            subproc.notify()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        messages = collect_queue_messages(message_queue)
        # We only print "Trapped" on posix, since we haven't implemented windows signals yet.
        assert sys.platform.startswith("win") or ("Trapped" in messages)
        # Check for the first message that would print
        assert "Log from test 0" in messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in messages
        assert subproc.exit_code != 0

    @pytest.mark.usefixtures("posix_target_user")
    def test_terminate_ends_process(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        posix_target_user: PosixSessionUser,
    ) -> None:
        # Make sure that the subprocess is forcefully killed when terminated

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        shutil.chown(python_app_loc, group=posix_target_user.group)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
            user=posix_target_user,
        )

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            time.sleep(1)
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        messages = collect_queue_messages(message_queue)
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in messages
        # Check for the first message that would print
        assert "Log from test 0" in messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in messages
        assert subproc.exit_code != 0

    @pytest.mark.usefixtures("posix_target_user")
    def test_terminate_ends_process_tree(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        posix_target_user: PosixSessionUser,
    ) -> None:
        # Make sure that the subprocess and all of its children are forcefully killed when terminated
        from psutil import Process, NoSuchProcess

        # GIVEN
        logger = build_logger(queue_handler)
        script_loc = (Path(__file__).parent / "support_files" / "app_20s_run.sh").resolve()
        shutil.chown(script_loc, group=posix_target_user.group)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[str(script_loc), sys.executable],
            user=posix_target_user,
        )

        def end_proc():
            time.sleep(1)
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            subproc.wait_until_started()
            children = list[Process]()
            attempt = 0
            while len(children) == 0 and attempt < 50:
                time.sleep(0.25)
                children = Process(subproc.pid).children(recursive=True)
                attempt += 1
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        messages = collect_queue_messages(message_queue)
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in messages
        # Check for the first message that would print
        assert "Log from test 0" in messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in messages
        assert subproc.exit_code != 0
        assert len(children) == 2  # .sh & .py scripts parented under 'sudo'
        num_children_running = 0
        for _ in range(0, 50):
            time.sleep(0.25)  # Give the child processes some time to end.
            num_children_running = 0
            for child in children:
                try:
                    # Raises NoSuchProcess if the process is gone
                    child.status()
                    num_children_running += 1
                except NoSuchProcess:
                    # Expected. This is a success
                    pass
            if num_children_running == 0:
                break
        assert num_children_running == 0
