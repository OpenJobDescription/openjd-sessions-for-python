# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for LoggingSubprocess"""

import sys
import tempfile
import time
import os
import getpass
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, wait
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from unittest.mock import MagicMock, patch
import pytest

import openjd
from openjd.sessions._os_checker import is_macos, is_posix, is_windows
from openjd.sessions._session_user import PosixSessionUser, WindowsSessionUser
from openjd.sessions._subprocess import LoggingSubprocess
from openjd.sessions import _subprocess as subprocess_impl_mod

from .conftest import (
    serial_process,
    build_logger,
    collect_queue_messages,
    has_posix_target_user,
    has_windows_user,
    are_tests_in_windows_session_0,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)


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

    def test_getters_return_none(self, queue_handler: QueueHandler, python_exe: str) -> None:
        # Check that the getters all return None if the subprocess hasn't run yet.

        # GIVEN
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", 'print("Test")'],
        )

        # THEN
        assert subproc.pid is None
        assert subproc.exit_code is None
        assert not subproc.is_running

    @pytest.mark.parametrize("exitcode", [0, 1])
    def test_basic_operation(
        self,
        exitcode: int,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Can we run a process, capture its output, and discover its return code?

        # GIVEN
        logger = build_logger(queue_handler)
        message = "this is 'output'"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", f'import sys; print("{message}"); sys.exit({exitcode})'],
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

    @pytest.mark.skipif(not is_posix(), reason="posix-specific test")
    @pytest.mark.parametrize("exitcode", [0, 1])
    def test_basic_operation_with_sameuser(
        self,
        exitcode: int,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # If the SessionUser is the process owner, then do we still run correctly.
        # Note: PosixSessionUser autopopulates the group if it's not given.

        # GIVEN
        current_user = getpass.getuser()
        user = PosixSessionUser(user=current_user)

        logger = build_logger(queue_handler)
        message = "this is output"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", f'import sys; print("{message}"); sys.exit({exitcode})'],
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

    def test_non_utf8_output_does_not_kill_reader(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # A child process that emits bytes that are not valid UTF-8 (e.g. a Windows
        # DCC application writing cp1252 to stdout) must not crash the stdout reader
        # thread. If the reader dies, all subsequent output is silently lost.
        # Regression test for the customer-reported UnicodeDecodeError on byte 0x97
        # (cp1252 em dash from Unreal Engine).

        # GIVEN
        logger = build_logger(queue_handler)
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'before\\n'); "
            "sys.stdout.buffer.flush(); "
            "sys.stdout.buffer.write(b'bad \\x97 byte\\n'); "
            "sys.stdout.buffer.flush(); "
            "sys.stdout.buffer.write(b'after\\n'); "
            "sys.stdout.buffer.flush()"
        )
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", script],
        )

        # WHEN
        subproc.run()

        # THEN
        assert subproc.exit_code == 0
        messages = collect_queue_messages(message_queue)
        assert "before" in messages
        # "after" is only logged if the reader thread survived the undecodable byte.
        assert "after" in messages

    @pytest.mark.parametrize(
        argnames=("raw_bytes", "expected_escaped"),
        argvalues=[
            # 0x97 is the em dash in cp1252 — the exact byte from the customer report
            # (Unreal Engine on Windows writing cp1252 to stdout).
            (b"bad \x97 byte", "bad \\x97 byte"),
            # 0xff is never valid anywhere in UTF-8.
            (b"bad \xff byte", "bad \\xff byte"),
            # Consecutive invalid bytes must each be escaped separately.
            (b"bad \xc7\xff bytes", "bad \\xc7\\xff bytes"),
            # cp1252-encoded text run — an invalid two-byte sequence in UTF-8.
            (b"bad \xc7\xe9 text", "bad \\xc7\\xe9 text"),
            # A truncated UTF-8 multi-byte sequence (0xe4 0xbd is an incomplete
            # 3-byte sequence) followed by valid ASCII.
            (b"truncated \xe4\xbd then ok", "truncated \\xe4\\xbd then ok"),
        ],
        ids=[
            "cp1252-em-dash",
            "invalid-byte",
            "consecutive-invalid",
            "cp1252-text",
            "truncated-utf8",
        ],
    )
    def test_non_utf8_output_is_escaped(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
        raw_bytes: bytes,
        expected_escaped: str,
    ) -> None:
        # Undecodable bytes in subprocess output must be escaped with backslashreplace
        # (e.g. b"\x97" -> "\\x97") so the original byte values are preserved in the
        # logs. Preserving the byte values (rather than collapsing to U+FFFD) helps
        # identify the codepage the subprocess is emitting.

        # GIVEN
        logger = build_logger(queue_handler)
        # The trailing "!" proves the full line was logged, not truncated at the bad
        # byte; the newline terminates the line for the reader's readline().
        payload = raw_bytes + b"!\n"
        script = (
            "import sys; " f"sys.stdout.buffer.write({payload!r}); " "sys.stdout.buffer.flush()"
        )
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", script],
        )

        # WHEN
        subproc.run()

        # THEN
        assert subproc.exit_code == 0
        messages = collect_queue_messages(message_queue)
        # The trailing "!" proves the full line was logged, not truncated at the bad byte.
        assert expected_escaped + "!" in messages
        # The replacement character must not appear; the byte value must be preserved.
        replaced = [m for m in messages if "\ufffd" in m]
        assert not replaced

    def test_valid_utf8_is_not_escaped(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Valid multi-byte UTF-8 sequences must pass through unmodified — escaping
        # applies only to genuinely invalid sequences.

        # GIVEN
        logger = build_logger(queue_handler)
        message = "héllo wörld Ç 星期五"
        # Pass the payload base64-encoded so the "Running command" log line does not
        # itself contain backslash-x escape sequences (from the bytes repr), which
        # would defeat the "no escapes appeared" assertion below.
        payload_b64 = b64encode(message.encode("utf-8")).decode("ascii")
        script = (
            "import sys, base64; "
            f"sys.stdout.buffer.write(base64.b64decode('{payload_b64}') + b'\\n'); "
            "sys.stdout.buffer.flush()"
        )
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", script],
        )

        # WHEN
        subproc.run()

        # THEN
        assert subproc.exit_code == 0
        messages = collect_queue_messages(message_queue)
        assert message in messages
        # Scope the escape sweep to subprocess output lines. The "Running command"
        # log line contains the interpreter path, which on Windows contains
        # backslashes that could false-positive this assertion.
        output_messages = [m for m in messages if not m.startswith("Running command")]
        escaped = [m for m in output_messages if "\\x" in m]
        assert not escaped
        replaced = [m for m in output_messages if "\ufffd" in m]
        assert not replaced

    def test_non_utf8_output_is_escaped_with_non_default_encoding(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # The escape behavior must hold for any configured encoding, not only the
        # utf-8 default: bytes that are invalid in that encoding are escaped, and
        # bytes that are valid in it decode normally.

        # GIVEN
        logger = build_logger(queue_handler)
        # 0x81 is undefined in cp1252 and must be escaped; 0x97 is the em dash in
        # cp1252 and must decode to U+2014.
        payload = b"undef \x81 byte, dash \x97 ok!\n"
        script = (
            "import sys; " f"sys.stdout.buffer.write({payload!r}); " "sys.stdout.buffer.flush()"
        )
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", script],
            encoding="cp1252",
        )

        # WHEN
        subproc.run()

        # THEN
        assert subproc.exit_code == 0
        messages = collect_queue_messages(message_queue)
        assert "undef \\x81 byte, dash \u2014 ok!" in messages

    def test_popen_uses_backslashreplace_error_handler(self) -> None:
        # Pin the errors= kwarg on the Popen construction itself. This protects all
        # construction paths that reuse the shared popen_args dict (same-user Popen,
        # the posix sudo path, and PopenWindowsAsUser, which CI integration tests
        # cannot all reach) against a refactor that moves or drops the kwarg.

        # GIVEN
        logger = MagicMock()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", "pass"],
        )

        # WHEN
        with patch.object(subprocess_impl_mod, "Popen") as mock_popen:
            subproc._start_subprocess()

        # THEN
        mock_popen.assert_called_once()
        kwargs = mock_popen.call_args.kwargs
        assert kwargs["errors"] == "backslashreplace"
        assert kwargs["encoding"] == "utf-8"

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

    def test_captures_stderr(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        # Ensure that messages sent to stderr are logged

        # GIVEN
        logger = build_logger(queue_handler)
        message = "this is output"
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", f'import sys; print("{message}", file=sys.stderr)'],
        )

        # WHEN
        subproc.run()

        # THEN
        messages = collect_queue_messages(message_queue)
        assert message in messages

    def test_cannot_run_twice(self, queue_handler: QueueHandler, python_exe: str) -> None:
        # We should fail if we try to run a LoggingSubprocess twice

        # GIVEN
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, "-c", "print('Test')"],
        )

        # WHEN
        subproc.run()

        # THEN
        with pytest.raises(RuntimeError):
            subproc.run()

    def test_invokes_callback(self, queue_handler: QueueHandler, python_exe: str) -> None:
        # Make sure that the given callback is invoked when the process exits.

        # GIVEN
        logger = build_logger(queue_handler)
        callback_mock = MagicMock()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                python_exe,
                "-c",
                "print('This is just a test')",
            ],
            callback=callback_mock,
        )

        # WHEN
        subproc.run()

        # THEN
        callback_mock.assert_called_once()

    @serial_process
    def test_notify_ends_process(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        # Make sure that process is sent a notification signal

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, str(python_app_loc)],
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.notify()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
        assert "Trapped" in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
        assert subproc.exit_code != 0

    @serial_process
    def test_terminate_ends_process(
        self, message_queue: SimpleQueue, queue_handler: QueueHandler, python_exe: str
    ) -> None:
        # Make sure that the subprocess is forcefully killed when terminated

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[python_exe, str(python_app_loc)],
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
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
    @serial_process
    def test_terminate_ends_process_tree(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        python_exe: str,
    ) -> None:
        # Make sure that the subprocess and all of its children are forcefully killed when terminated
        from psutil import Process, NoSuchProcess, STATUS_ZOMBIE

        # GIVEN
        logger = build_logger(queue_handler)
        script_loc = (Path(__file__).parent / "support_files" / "run_app_20s_run.py").resolve()
        args = [python_exe, str(script_loc)]
        subproc = LoggingSubprocess(logger=logger, args=args)
        children = []
        all_messages = []
        # Note: This is the number of *CHILD* processes of the main process that we start.
        #  The total number of processes in flight will be this plus one.

        # On Posix and on Windows not in a virtual environment:
        # Process tree: python -> python
        # Children: python
        expected_num_child_procs = 1

        # Check if we're in a virtual environment on Windows, see https://docs.python.org/3/library/venv.html#how-venvs-work
        if is_windows() and sys.prefix != sys.base_prefix:
            # Windows starts an extra python process due to running in a virtual environment
            # Process tree: conhost -> python -> python -> python
            # Children: python, python, python
            expected_num_child_procs = 3
        elif is_windows() and are_tests_in_windows_session_0():
            # When running as a service there's an additional process that gets added
            # Process tree: conhost -> python -> python
            # Children: python, python
            expected_num_child_procs = 2

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            children.extend(Process(subproc.pid).children(recursive=True))
            for child in children:
                logger.info(f"Child {child.name()} -- {str(child)}")
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        all_messages.extend(collect_queue_messages(message_queue))
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 19, then we ended before the app naturally finished.
        assert "Log from test 19" not in all_messages
        assert subproc.exit_code != 0
        assert len(children) == expected_num_child_procs

        num_children_running = 0
        for _ in range(0, 50):
            time.sleep(0.25)  # Give the child process some time to end.
            num_children_running = 0
            for child in children:
                try:
                    # Raises NoSuchProcess if the process is gone
                    child_status = child.status()
                    if child_status != STATUS_ZOMBIE:
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
        python_exe: str,
    ) -> None:
        # Make sure the run method reads up to a max line length

        # GIVEN
        expected_max_line_length = 64 * 1000
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                python_exe,
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

    @pytest.mark.parametrize(
        "command, expected_message_indices",
        [
            pytest.param(
                command,
                indices,
                marks=pytest.mark.skipif(
                    s == 10 and os.name == "nt",
                    reason="Crashes runners on windows with xdist and cov",
                ),
            )
            for s, indices in [
                (0, [0, 3]),
                (3, [0, 1, 3]),
                (10, [0, 1, 2, 3]),
            ]
            for command in [
                f'["python", "-c", "import time; [print(\\"Hello World {{i}}\\") for i in range({s}) if not time.sleep(1)]"]',
                f'["python", "-c", "import time; time.sleep({s})"]',
            ]
        ],
    )
    @pytest.mark.timeout(
        7
    )  # This test timing out could indicate a regression on the "actions end when subproc exits" behavior
    def test_run_gracetime_when_process_ends_but_grandchild_uses_stdout(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        command: str,
        expected_message_indices: list[int],
        python_exe: str,
    ) -> None:
        # Make sure that the run command ends when the main subprocess ends
        # GIVEN
        logger = build_logger(queue_handler)
        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                python_exe,
                "-c",
                f'import subprocess;process = subprocess.Popen({command}, encoding="utf-8")',
            ],
        )

        # WHEN
        subproc.run()

        # THEN
        messages = collect_queue_messages(message_queue)

        # we have to construct it here because it depends on the subproc PID
        sample_messages = [
            f"Command started as pid: {subproc.pid}",
            "Command exited but STDOUT stream is still open. Waiting gracetime of 5 seconds for the STDOUT stream to close before ending action.",
            "Gracetime of 5 seconds elapsed but the STDOUT stream is still open. Ending action.",
            f"Process pid {subproc.pid} exited with code: 0 (unsigned) / 0x0 (hex)",
        ]
        expected_messages = [
            m for i, m in enumerate(sample_messages) if i in expected_message_indices
        ]
        not_expected_messages = [
            m for i, m in enumerate(sample_messages) if i not in expected_message_indices
        ]

        assert list_has_items_in_order(expected_messages, messages)
        assert all(
            m not in messages for m in not_expected_messages
        ), f"Unexpected messages: {', '.join(repr(m) for m in not_expected_messages if m in messages)}"

    @pytest.mark.skipif(is_windows(), reason="Posix-specific tests")
    def test_creation_flags_posix(self, queue_handler: QueueHandler) -> None:

        with pytest.raises(ValueError):
            logger = build_logger(queue_handler)
            LoggingSubprocess(
                logger=logger,
                args=[sys.executable, "-c", 'print("this should not run")'],
                # Creation flags aren't supported on Posix systems.
                creation_flags=1337,
            )


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
    reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)
@pytest.mark.usefixtures("message_queue", "queue_handler", "posix_target_user")
class TestLoggingSubprocessPosixCrossUser(object):
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
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
            user=posix_target_user,
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.notify()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
        # We only print "Trapped" on posix, since we haven't implemented windows signals yet.
        assert sys.platform.startswith("win") or ("Trapped" in all_messages)
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
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
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(python_app_loc)],
            user=posix_target_user,
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
        assert subproc.exit_code != 0

    @pytest.mark.usefixtures("posix_target_user")
    def test_terminate_ends_process_tree(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        posix_target_user: PosixSessionUser,
    ) -> None:
        # Make sure that the subprocess and all of its children are forcefully killed when terminated
        from psutil import Process, NoSuchProcess, STATUS_ZOMBIE

        # GIVEN
        logger = build_logger(queue_handler)
        script_loc = (Path(__file__).parent / "support_files" / "run_app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, str(script_loc)],
            user=posix_target_user,
        )
        children = []
        all_messages = []
        # python, python
        expected_num_child_procs: int = 2

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            children.extend(Process(subproc.pid).children(recursive=True))
            for child in children:
                logger.info(f"Child {child.name()} -- {str(child)}")
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        all_messages.extend(collect_queue_messages(message_queue))
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
        assert subproc.exit_code != 0
        assert len(children) == expected_num_child_procs
        num_children_running = 0
        for _ in range(0, 50):
            time.sleep(0.25)  # Give the child processes some time to end.
            num_children_running = 0
            for child in children:
                try:
                    # Raises NoSuchProcess if the process is gone
                    child_status = child.status()
                    if child_status != STATUS_ZOMBIE:
                        num_children_running += 1
                except NoSuchProcess:
                    # Expected. This is a success
                    pass
            if num_children_running == 0:
                break
        assert num_children_running == 0


@pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
@pytest.mark.xfail(
    not has_windows_user(),
    reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
)
class TestLoggingSubprocessWindowsCrossUser(object):
    """Tests for LoggingSubprocess's ability to run the subprocess as a separate user on Windows."""

    def test_basic_operation_success(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Test that we run the subprocess as a desired user that differs from the current user.

        # GIVEN
        logger = build_logger(queue_handler)
        exitcode = 0

        subproc = LoggingSubprocess(
            logger=logger,
            args=["whoami"],
            user=windows_user,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == exitcode
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)
        print(messages)
        assert any(windows_user.user in message for message in messages)

    @pytest.mark.skipif(not is_windows(), reason="Windows-specific tests")
    @pytest.mark.xfail(
        not has_windows_user(),
        reason=WIN_SET_TEST_ENV_VARS_MESSAGE,
    )
    def test_environment_casing(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Do the imports here as these are only available on Windows
        if is_windows():
            from ctypes import c_void_p
            from openjd.sessions._win32._helpers import environment_block_to_dict  # type: ignore

        # Test that we run the subprocess as a desired user that differs from the current user.

        # GIVEN
        logger = build_logger(queue_handler)
        exitcode = 0

        # Powershell script that ensures that all environment variables are upper case.
        # Explicitly not using Python as it reads in all enivronment variables as upper case
        # on Windows and we want to make sure that we actually made all environment variables
        # upper case in the process.
        powershell_script = r"""
$allEnvVars = Get-ChildItem env:

foreach ($envVar in $allEnvVars) {
    $varName = $envVar.Name
    $varValue = $envVar.Value
    echo "$varName=$varValue"

    if ($varName -cne $varName.ToUpper()) {
        Write-Error "Environment variable '$varName' is not in all uppercase."
    }
}"""

        def _inject_value_to_user_dict(block: c_void_p) -> dict[str, str]:
            """
            Inject a lower case environment key to make sure that we have at least one
            system environment variable that needs to be changed to upper case
            """
            user_dict = environment_block_to_dict(block)
            user_dict["test_user_dict"] = "test_user_dict_value"
            user_dict["Testdup"] = "this_is_an_original_value"
            return user_dict

        with patch(
            f"{openjd.__package__}.sessions._win32._popen_as_user.environment_block_to_dict",
            side_effect=_inject_value_to_user_dict,
        ):
            subproc = LoggingSubprocess(
                logger=logger,
                # Print out the environment
                args=["powershell", "-Command", powershell_script],
                user=windows_user,
                # Throw a lower case environment variable in there to guarantee
                # that there's one that needs to be changed.
                os_env_vars={"testenv": "this_is_a_test", "TESTDUP": "this_is_a_changed_value"},
            )

            # WHEN
            subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == exitcode
        messages = collect_queue_messages(message_queue)

        assert "TEST_USER_DICT=test_user_dict_value" in messages
        assert "TESTDUP=this_is_a_changed_value" in messages
        assert "TESTENV=this_is_a_test" in messages

        assert "test_user_dict=test_user_dict_value" not in messages
        assert "Testdup=this_is_an_original_value" not in messages
        assert "testenv=this_is_a_test" not in messages

    def test_basic_operation_failure(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Test that we run the subprocess as a desired user that differs from the current user.

        # GIVEN
        logger = build_logger(queue_handler)

        subproc = LoggingSubprocess(
            logger=logger,
            args=[
                "powershell",
                "-Command",
                "whoami; exit 1",
            ],
            user=windows_user,
        )

        # WHEN
        subproc.run()

        # THEN
        assert not subproc.is_running
        assert subproc.pid is not None
        assert subproc.exit_code == 1
        assert message_queue.qsize() > 0
        messages = collect_queue_messages(message_queue)
        print(messages)
        assert any(windows_user.user in message for message in messages)

    def test_notify_ends_process(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Make sure that process is sent a notification signal
        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            args=["python", str(python_app_loc)],
            user=windows_user,
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.notify()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
        assert "Trapped" in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 19, then we ended before the app naturally finished.
        assert "Log from test 19" not in all_messages
        assert subproc.exit_code != 0

    def test_terminate_ends_process(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Make sure that the subprocess is forcefully killed when terminated

        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()

        subproc = LoggingSubprocess(
            logger=logger,
            args=["python", str(python_app_loc)],
            user=windows_user,
        )
        all_messages = []

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THEN
        assert not subproc.is_running
        all_messages.extend(collect_queue_messages(message_queue))
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 19, then we ended before the app naturally finished.
        assert "Log from test 19" not in all_messages
        assert subproc.exit_code != 0

    def test_terminate_ends_process_tree(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Make sure that the subprocess and all of its children are forcefully killed when terminated
        from psutil import Process, NoSuchProcess

        # GIVEN
        logger = build_logger(queue_handler)

        script_loc = (Path(__file__).parent / "support_files" / "run_app_20s_run.py").resolve()
        subproc = LoggingSubprocess(
            logger=logger,
            # Use the default 'python' rather than 'sys.executable' since we typically do not have access to
            # sys.executable when running with impersonation since it's in a hatch environment for the local user.
            args=["python", str(script_loc)],
            user=windows_user,
        )
        children = []
        all_messages = []
        # conhost, python
        expected_num_child_procs: int = 2

        def end_proc():
            subproc.wait_until_started()
            # Then give the Python subprocess some time to finish loading and start running.
            for _ in range(20):
                all_messages.extend(collect_queue_messages(message_queue))
                if "Log from test 0" not in all_messages:
                    time.sleep(1)
                else:
                    break
            children.extend(Process(subproc.pid).children(recursive=True))
            for child in children:
                logger.info(f"Child {child.name()} -- {str(child)}")
            subproc.terminate()

        # WHEN
        with ThreadPoolExecutor(max_workers=2) as pool:
            future1 = pool.submit(subproc.run)
            future2 = pool.submit(end_proc)
            wait((future1, future2), return_when="ALL_COMPLETED")

        # THENs
        all_messages.extend(collect_queue_messages(message_queue))
        # If we printed "Trapped" then we hit our signal handler, and that shouldn't happen.
        assert "Trapped" not in all_messages
        # Check for the first message that would print
        assert "Log from test 0" in all_messages
        # If there's no 9, then we ended before the app naturally finished.
        assert "Log from test 9" not in all_messages
        assert subproc.exit_code != 0
        assert len(children) == expected_num_child_procs
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


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestLoggingSubprocessMacOSSetsid:
    """Tests for the macOS-specific cross-user command construction.

    macOS has no setsid(1), so on darwin the workload is launched under a small
    pure-Python shim (run via a system-location Python interpreter with -I) that
    becomes a new session/process-group leader before exec'ing the real command.
    """

    @pytest.mark.skipif(
        is_windows(), reason="Constructs a PosixSessionUser, which is rejected on Windows hosts"
    )
    def test_builds_setsid_shim_command_on_macos(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        from openjd.sessions import _subprocess as subprocess_mod

        logger = build_logger(queue_handler)
        target_user = MagicMock(spec=PosixSessionUser)
        target_user.user = "job-user"
        target_user.is_process_user.return_value = False
        subproc = LoggingSubprocess(
            logger=logger,
            args=["/path/to/workload.sh"],
            user=target_user,
        )

        # WHEN
        with (
            patch.object(subprocess_mod, "is_macos", return_value=True),
            patch.object(subprocess_mod, "is_posix", return_value=True),
            patch.object(subprocess_mod, "is_windows", return_value=False),
            patch.object(
                subprocess_mod, "_macos_shim_interpreter", return_value="/usr/local/bin/python3"
            ),
            patch.object(subprocess_mod, "Popen") as mock_popen,
        ):
            subproc._start_subprocess()

        # THEN
        built_command = mock_popen.call_args.kwargs["args"]
        assert built_command == [
            "sudo",
            "-u",
            "job-user",
            "-i",
            "/usr/local/bin/python3",
            "-I",
            "-c",
            subprocess_mod._MACOS_SETSID_SHIM,
            "/path/to/workload.sh",
        ]


@pytest.mark.skipif(not is_posix(), reason="process groups and setsid are posix-only")
class TestSetsidShimBehavior:
    """Tests the behaviour of the setsid shim string itself, on any POSIX host.

    The shim is portable POSIX (os.getpgrp/os.getpid/os.setsid/os.execvp with no
    platform branch); macOS is merely the platform where it is *required*, because
    macOS ships no setsid(1) for the cross-user command to call. Running it
    everywhere POSIX is deliberate: Linux exercises the same semantics on faster,
    more reliable runners, so a broken shim string is caught there too rather than
    only in the macOS job.

    Whether macOS actually *selects* this shim is a separate concern, covered by
    TestLoggingSubprocessMacOSSetsid::test_builds_setsid_shim_command_on_macos.
    """

    def test_setsid_shim_creates_new_process_group(self) -> None:
        # GIVEN the shim string that macOS uses in place of setsid(1).
        from subprocess import PIPE, run

        from openjd.sessions import _subprocess as subprocess_mod

        # WHEN we run it (as the current user; no sudo) to report the workload's
        # process-group id alongside the launching python's own pid.
        result = run(
            [
                sys.executable,
                "-I",
                "-c",
                subprocess_mod._MACOS_SETSID_SHIM,
                "/bin/sh",
                "-c",
                "echo $$ $(ps -o pgid= -p $$)",
            ],
            stdout=PIPE,
            text=True,
            check=True,
        )

        # THEN the workload is the leader of its own process group (pgid == its pid).
        workload_pid, workload_pgid = (int(x) for x in result.stdout.split())
        assert workload_pid == workload_pgid


@pytest.mark.skipif(not is_macos(), reason="macOS-specific interpreter selection")
class TestMacOSShimInterpreter:
    """Tests for _macos_shim_interpreter(), which selects the Python interpreter that runs
    the setsid shim as the jobRunAsUser, and for the _other_users_can_execute() permission
    check that backs it.

    Unlike the shim string, this selection logic is genuinely macOS-only (it exists to
    find an interpreter the job user can execute, outside the agent's venv), so these
    are scoped to macOS hosts."""

    def test_prefers_base_executable(self, tmp_path: Path) -> None:
        # GIVEN a reachable interpreter behind sys._base_executable
        from openjd.sessions import _subprocess as subprocess_mod

        interpreter = tmp_path / "python3"
        interpreter.touch()

        # WHEN
        with (
            patch.object(subprocess_mod.sys, "_base_executable", str(interpreter), create=True),
            patch.object(subprocess_mod, "_other_users_can_execute", return_value=True),
        ):
            result = subprocess_mod._macos_shim_interpreter()

        # THEN
        assert result == str(interpreter.resolve())

    def test_resolves_symlink_to_base_interpreter(self, tmp_path: Path) -> None:
        # GIVEN _base_executable is a symlink (e.g. a framework/Homebrew shim)
        from openjd.sessions import _subprocess as subprocess_mod

        real_interpreter = tmp_path / "python3.11"
        real_interpreter.touch()
        link = tmp_path / "python3"
        link.symlink_to(real_interpreter)

        # WHEN
        with (
            patch.object(subprocess_mod.sys, "_base_executable", str(link), create=True),
            patch.object(subprocess_mod, "_other_users_can_execute", return_value=True),
        ):
            result = subprocess_mod._macos_shim_interpreter()

        # THEN the symlink is resolved to the real interpreter
        assert result == str(real_interpreter.resolve())

    def test_uses_sys_executable_when_base_executable_unset(self, tmp_path: Path) -> None:
        # GIVEN _base_executable is None (not a venv); sys.executable is used instead
        from openjd.sessions import _subprocess as subprocess_mod

        interpreter = tmp_path / "python3"
        interpreter.touch()

        # WHEN
        with (
            patch.object(subprocess_mod.sys, "_base_executable", None, create=True),
            patch.object(subprocess_mod.sys, "executable", str(interpreter)),
            patch.object(subprocess_mod, "_other_users_can_execute", return_value=True),
        ):
            result = subprocess_mod._macos_shim_interpreter()

        # THEN
        assert result == str(interpreter.resolve())

    def test_falls_back_when_base_not_executable_by_others(self, tmp_path: Path) -> None:
        # GIVEN the base interpreter is not reachable by other users, but the fallback is
        from openjd.sessions import _subprocess as subprocess_mod

        interpreter = tmp_path / "python3"
        interpreter.touch()

        def reachable(path: str) -> bool:
            return path == subprocess_mod._MACOS_FALLBACK_SHIM_INTERPRETER

        # WHEN
        with (
            patch.object(subprocess_mod.sys, "_base_executable", str(interpreter), create=True),
            patch.object(subprocess_mod, "_other_users_can_execute", side_effect=reachable),
        ):
            result = subprocess_mod._macos_shim_interpreter()

        # THEN
        assert result == subprocess_mod._MACOS_FALLBACK_SHIM_INTERPRETER

    def test_raises_actionable_error_when_no_interpreter_is_reachable(self, tmp_path: Path) -> None:
        """With neither candidate reachable, fail at selection with an explanation.

        Returning the fallback unchecked would defer the failure to Popen, which reports
        only "[Errno 2] No such file or directory: '/usr/bin/python3'" on a workload that
        may have nothing to do with Python. The message is asserted rather than just the
        exception type, because it is the only thing the operator sees: the caller logs
        str(e) and returns None rather than propagating.
        """
        # GIVEN neither the base interpreter nor the fallback is reachable
        from openjd.sessions import _subprocess as subprocess_mod

        interpreter = tmp_path / "python3"
        interpreter.touch()

        # WHEN
        with (
            patch.object(subprocess_mod.sys, "_base_executable", str(interpreter), create=True),
            patch.object(subprocess_mod, "_other_users_can_execute", return_value=False),
        ):
            with pytest.raises(subprocess_mod.NoReachableInterpreterError) as excinfo:
                subprocess_mod._macos_shim_interpreter()

        # THEN the message names both rejected candidates and the remedy
        message = str(excinfo.value)
        assert str(interpreter) in message, "the rejected base interpreter must be named"
        assert subprocess_mod._MACOS_FALLBACK_SHIM_INTERPRETER in message
        assert "xcode-select --install" in message, "the remedy must be actionable"
        # And it explains WHY an interpreter is involved at all, since the workload
        # being launched may have nothing to do with Python.
        assert "setsid" in message

    def test_no_reachable_interpreter_reaches_the_operator_as_a_start_failure(
        self, tmp_path: Path, queue_handler: QueueHandler, message_queue: SimpleQueue
    ) -> None:
        """The raised message must survive the path back to the operator.

        _start_subprocess catches Exception, logs "Process failed to start: {e}" and
        returns None; nothing re-raises. So the message text is the entire diagnostic,
        and this pins that it is not swallowed or replaced along the way.
        """
        # GIVEN a cross-user launch on macOS with no reachable interpreter
        from openjd.sessions import _subprocess as subprocess_mod

        logger = build_logger(queue_handler)
        target_user = MagicMock(spec=PosixSessionUser)
        target_user.user = "job-user"
        target_user.is_process_user.return_value = False
        subproc = LoggingSubprocess(
            logger=logger,
            args=["/path/to/workload.sh"],
            user=target_user,
        )

        # WHEN
        with (
            patch.object(subprocess_mod, "is_macos", return_value=True),
            patch.object(subprocess_mod, "is_posix", return_value=True),
            patch.object(subprocess_mod, "is_windows", return_value=False),
            patch.object(subprocess_mod, "_other_users_can_execute", return_value=False),
        ):
            result = subproc._start_subprocess()

        # THEN the launch fails and the operator gets the actionable message
        assert result is None
        messages = collect_queue_messages(message_queue)
        assert any(
            "Process failed to start" in m and "xcode-select --install" in m for m in messages
        ), f"actionable message did not reach the log; got: {messages}"

    @pytest.mark.skipif(not is_posix(), reason="POSIX permission-bit semantics")
    def test_other_users_can_execute_system_binary(self) -> None:
        # GIVEN a system binary that is world-executable with world-traversable parents
        from openjd.sessions import _subprocess as subprocess_mod

        # THEN
        assert subprocess_mod._other_users_can_execute("/bin/sh")

    @pytest.mark.skipif(is_windows(), reason="POSIX permission bits are not honored on Windows")
    def test_other_users_cannot_execute_without_o_x_bit(self, tmp_path: Path) -> None:
        # GIVEN a file that other users cannot execute (no o+x bit)
        from openjd.sessions import _subprocess as subprocess_mod

        interpreter = tmp_path / "python3"
        interpreter.touch()
        interpreter.chmod(0o750)

        # THEN
        assert not subprocess_mod._other_users_can_execute(str(interpreter))

    @pytest.mark.skipif(is_windows(), reason="POSIX permission bits are not honored on Windows")
    def test_other_users_cannot_execute_behind_private_dir(self, tmp_path: Path) -> None:
        # GIVEN a world-executable file inside a directory that other users cannot
        # traverse (e.g. a Python install under a 0o750 home directory)
        from openjd.sessions import _subprocess as subprocess_mod

        private_dir = tmp_path / "private"
        private_dir.mkdir()
        interpreter = private_dir / "python3"
        interpreter.touch()
        interpreter.chmod(0o755)
        private_dir.chmod(0o750)

        # WHEN
        try:
            result = subprocess_mod._other_users_can_execute(str(interpreter))
        finally:
            # Restore so pytest can clean up tmp_path
            private_dir.chmod(0o755)

        # THEN
        assert not result

    def test_other_users_cannot_execute_missing_path(self, tmp_path: Path) -> None:
        # GIVEN a path that does not exist
        from openjd.sessions import _subprocess as subprocess_mod

        # THEN
        assert not subprocess_mod._other_users_can_execute(str(tmp_path / "no-such-python"))


class TestFastExitingChild:
    """A trivial command can exit before the runner finishes recording it.

    Looking up an already-reaped child's process group raises ProcessLookupError
    on posix, and psutil raises NoSuchProcess when walking it on Windows. Neither
    may fail the action: the child ran, and its exit code is still collected.
    """

    @pytest.mark.skipif(not is_posix(), reason="posix-only: process groups")
    @pytest.mark.usefixtures("message_queue", "queue_handler")
    def test_getpgid_lookup_failure_does_not_fail_the_action(
        self,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # GIVEN: the child is gone by the time its process group is looked up
        logger = build_logger(queue_handler)
        callback = MagicMock()
        subproc = LoggingSubprocess(
            logger=logger,
            args=[sys.executable, "-c", "print('DONE')"],
            callback=callback,
        )

        with patch.object(
            subprocess_impl_mod.os, "getpgid", side_effect=ProcessLookupError(3, "No such process")
        ):
            # WHEN
            subproc.run()

        # THEN: the action completed normally
        assert subproc.exit_code == 0
        assert subproc.failed_to_start is False
        messages = collect_queue_messages(message_queue)
        assert "DONE" in messages

    @pytest.mark.skipif(not is_windows(), reason="Windows-only: psutil process walk")
    def test_process_tree_walk_tolerates_exited_process(self) -> None:
        # GIVEN: a process that disappears between discovery and the walk
        from psutil import NoSuchProcess

        from openjd.sessions._windows_process_killer import _suspend_process_tree

        logger = MagicMock()
        process = MagicMock()
        process.pid = 4321
        process.suspend.side_effect = NoSuchProcess(4321)
        process.children.side_effect = NoSuchProcess(4321)
        cannot_suspend: list = []
        all_processes: list = []

        # WHEN / THEN: no exception escapes
        _suspend_process_tree(
            logger, process, all_processes, cannot_suspend, suspend_subprocesses=True
        )
        assert process in all_processes
