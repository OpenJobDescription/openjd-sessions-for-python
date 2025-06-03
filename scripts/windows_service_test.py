# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import socket
import logging
from threading import Event
from typing import Optional

import win32serviceutil
import win32service
import servicemanager
import subprocess
import sys
import os
import argparse
import shlex
import win32con
import win32api
from getpass import getpass


logger = logging.getLogger(__name__)


class OpenJDSessionsForPythonTestService(win32serviceutil.ServiceFramework):
    # Pywin32 Service Configuration
    _svc_name_ = "OpenJDSessionsForPythonTest"
    _svc_display_name_ = "OpenJD Sessions For Python Test"
    _exe_name_ = "OpenJDSessionsForPythonTestService.exe"

    _stop_event: Event

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)

        self._stop_event = Event()
        socket.setdefaulttimeout(60)

    def SvcStop(self):
        """Invoked when the Windows Service is being stopped"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logger.info("Windows Service is being stopped")
        self._stop_event.set()

    def SvcShutdown(self):
        """Invoked when the system is shutdown"""
        self.SvcStop()

    def SvcDoRun(self):
        """The main entrypoint called after the service is started"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        code_location = os.environ["CODE_LOCATION"]
        pytest_args = os.environ.get("PYTEST_ARGS", None)

        args = ["pytest", os.path.join(code_location, "test")]

        if pytest_args:
            args.extend(shlex.split(pytest_args, posix=False))

        logging.basicConfig(
            filename=os.path.join(code_location, "test.log"),
            encoding="utf-8",
            level=logging.INFO,
            filemode="w",
        )
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=code_location,
        )

        while True:
            output = process.stdout.readline()
            if not output and process.poll() is not None:
                break

            logger.info(output.strip())

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )
        logger.info("Stop status sent to Windows Service Controller")


def _install_service(username: str, pytest_args: Optional[str] = None) -> list[str]:
    if "\\" not in username and "@" not in username:
        username = f".\\{username}"

    password = getpass("Please enter the user's password:")
    args = ["--username", username, "--password", password, "install"]

    exe_args = [f"CODE_LOCATION={os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}"]

    if pytest_args:
        exe_args.append(f"PYTEST_ARGS={pytest_args}")

    key_handle = None
    try:
        # https://timgolden.me.uk/pywin32-docs/win32api__RegOpenKeyEx_meth.html
        key_handle = win32api.RegOpenKeyEx(
            getattr(win32con, "HKEY_LOCAL_MACHINE"),
            f"SYSTEM\\CurrentControlSet\\Services\\{OpenJDSessionsForPythonTestService._svc_name_}",
            0,  # reserved, only use 0
            win32con.KEY_SET_VALUE,
        )
        # https://timgolden.me.uk/pywin32-docs/win32api__RegSetValueEx_meth.html
        win32api.RegSetValueEx(
            key_handle,
            "Environment",
            0,  # reserved, only use 0,
            win32con.REG_MULTI_SZ,  # Multi-string value
            exe_args,
        )
    finally:
        if key_handle is not None:
            win32api.CloseHandle(key_handle)

    return args


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="Run OpenJD Sessions for Python Windows Service Tests",
    )

    # We wrap the commandline for win32serviceutil so that we can get the
    # password from stdin instead of plaintext on the command line.
    subparsers = parser.add_subparsers(dest="mode")

    install_service_args = subparsers.add_parser("install")
    install_service_args.add_argument("--username", required=True, type=str)
    install_service_args.add_argument(
        "--pytest-args",
        required=False,
        type=str,
        dest="pytest_args",
        default=None,
        help='Use this with an equals like --pytest-args="-vvv". Otherwise the argument parser will not recognize the dash at the beginning (-)',
    )

    subparsers.add_parser("run")

    args = parser.parse_args()

    argv = [sys.argv[0]]

    if args.mode == "install":
        username = args.username

        argv.extend(_install_service(username=username, pytest_args=args.pytest_args))

    elif args.mode == "run":
        argv.append("start")

    win32serviceutil.HandleCommandLine(OpenJDSessionsForPythonTestService, argv=argv)
