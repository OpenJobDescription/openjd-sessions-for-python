# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import subprocess
import sys
import time
from pathlib import Path
import psutil

from openjd.sessions._windows_process_killer import (
    _suspend_process_tree,
    _kill_processes,
    kill_windows_process_tree,
    _suspend_process,
)
from logging.handlers import QueueHandler
import pytest
from .conftest import build_logger
from subprocess import Popen


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestWindowsProcessKiller:
    def test_suspend_process_tree(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        process = Popen([sys.executable, python_app_loc], stdout=subprocess.PIPE, text=True)

        # When
        # Give a few seconds for running the python script
        time.sleep(3)
        proc = psutil.Process(process.pid)
        _suspend_process_tree(logger, proc, [], [], True)

        # Then
        try:
            assert proc.status() == psutil.STATUS_STOPPED
        finally:
            proc.kill()

    def test_suspend_process(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        process = Popen([sys.executable, python_app_loc], stdout=subprocess.PIPE, text=True)

        # When
        # Give a few seconds for running the python script
        time.sleep(3)
        proc = psutil.Process(process.pid)
        result = _suspend_process(logger, proc)

        # Then
        try:
            assert result
            assert proc.status() == psutil.STATUS_STOPPED
        finally:
            proc.kill()

    def test_kill_processes(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        process = Popen([sys.executable, python_app_loc], stdout=subprocess.PIPE, text=True)

        # When
        # Give a few seconds for running the python script
        time.sleep(3)
        proc = psutil.Process(process.pid)
        _kill_processes(logger, [proc])

        # Then
        assert not psutil.pid_exists(process.pid)

    def test_kill_windows_process_tree(self, queue_handler: QueueHandler) -> None:
        # GIVEN
        logger = build_logger(queue_handler)
        python_app_loc = (Path(__file__).parent / "support_files" / "app_20s_run.py").resolve()
        process = Popen([sys.executable, python_app_loc], stdout=subprocess.PIPE, text=True)

        # When
        # Give a few seconds for running the python script
        time.sleep(3)
        kill_windows_process_tree(logger, process.pid)

        # Then
        assert not psutil.pid_exists(process.pid)
