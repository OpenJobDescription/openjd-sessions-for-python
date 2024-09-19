# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from __future__ import annotations

from typing import Any, MutableMapping, TypedDict
from enum import Flag, auto
import logging

__all__ = ["LOG", "LoggerAdapter", "LogExtraInfo", "LogContent"]


class LogContent(Flag):
    """
    A Flag Enum which describes the content of a log record.
    """

    BANNER = auto()  # Logs which contain a banner, such as to visually seperate log sections
    FILE_PATH = auto()  # Logs which contain a filepath
    FILE_CONTENTS = auto()  # Logs which contain the contents of a file
    COMMAND_OUTPUT = auto()  # Logs which contain the output of a command run
    EXCEPTION_INFO = (
        auto()
    )  # Logs which contain an exception openjd encountered, potentially including sensitive information such as filepaths or host information.
    PROCESS_CONTROL = (
        auto()
    )  # Logs which contain information related to starting, killing, or signalling processes.
    PARAMETER_INFO = (
        auto()
    )  # Logs which contain details about parameters and their values pertaining to the running action
    HOST_INFO = (
        auto()
    )  # Logs which contain details about the system environment, e.g. dependency versions, OS name, CPU architecture.


class LogExtraInfo(TypedDict):
    """
    A TypedDict which contains extra information to be added to the "extra" key of a log record.
    """

    openjd_log_content: LogContent | None


class LoggerAdapter(logging.LoggerAdapter):
    """
    LoggerAdapter which merges the "extra" kwarg instead of replacing with what the LoggerAdapter was initialized with.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        """
        Typically the LoggerAdaptor simply replaces the `extra` key in the kwargs with the one initialized with the
        adapter. However, we want to merge the two dictionaries, so we override it here.
        """
        if "extra" not in kwargs:
            kwargs["extra"] = self.extra
        else:
            kwargs["extra"] |= self.extra
        return msg, kwargs


# Name the logger for the sessions module, rather than this specific file
LOG = logging.getLogger(".".join(__name__.split(".")[:-1]))
"""
The logger of the openjd sessions module. The logger has the name openjd.sessions and is used 
throughout the openjd sessions module to provide information on actions the module is taking, as
well as any output from commands run during Sessions.

Some LogRecords sent to the logger will have an extra attribute named "openjd_log_content" who's 
value is a LogContent which provides information on what data is contained in the LogRecord, or None
if there is no applicable LogContent field (for example, a message like "Ending Session")

If the LogRecord does not have the "openjd_log_content" no guarantees are made as to what content
is in the LogRecord. LogRecords that contain LogContent.EXECPTION_INFO may also transitively include
potentially sensitive information like filepaths or host info due to the nature of exception messages.
"""
LOG.setLevel(logging.INFO)

_banner_log_extra = LogExtraInfo(openjd_log_content=LogContent.BANNER)


def log_section_banner(logger: LoggerAdapter, section_title: str) -> None:
    logger.info("")
    logger.info("==============================================", extra=_banner_log_extra)
    logger.info(f"--------- {section_title}", extra=_banner_log_extra)
    logger.info("==============================================", extra=_banner_log_extra)


def log_subsection_banner(logger: LoggerAdapter, section_title: str) -> None:
    logger.info("----------------------------------------------", extra=_banner_log_extra)
    logger.info(section_title, extra=_banner_log_extra)
    logger.info("----------------------------------------------", extra=_banner_log_extra)
