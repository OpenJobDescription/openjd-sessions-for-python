# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
from dataclasses import dataclass
from typing import TypedDict
from enum import Enum, Flag, auto
import logging

__all__ = ["LOG", "LogMetadata", "OJDExtraInfo", "LogContent", "LogPurpose"]


class LogPurpose(Enum):
    DIAGNOSTIC = (
        "Diagnostic"  # OpenJD logs which describe something openjd itself is doing or encountered.
    )
    TITLE = "Title"  # Logs which serve to break up the total log by means of a title or break
    OUTPUT = (
        "Output"  # Logs which are Output of a running command, and not directly logged by openjd
    )


class LogContent(Flag):
    FILE_PATH = auto()  # Logs which contain a filepath
    FILE_CONTENTS = auto()  # Logs which contain the contents of a file
    COMMAND_OUTPUT = auto()  # Logs which contain the output of a command run
    EXCEPTION_INFO = auto()  # Logs which contain an exception openjd encountered
    PROCESS_IDS = auto()  # Logs which contain process IDs or details about process IDs
    PARAMETER_INFO = (
        auto()
    )  # Logs which contain details about parameters and their values pertaining to the running action
    ENVIRONMENT_DETAILS = (
        auto()
    )  # Logs which contain details about the system environment, e.g. dependency versions, OS name, CPU architecture.


@dataclass
class LogMetadata:
    log_purpose: LogPurpose
    log_content: LogContent = LogContent(0)  # Empty flag


class OJDExtraInfo(TypedDict):
    openjd_log_metadata: LogMetadata


# Name the logger for the sessions module, rather than this specific file
LOG = logging.getLogger(".".join(__name__.split(".")[:-1]))
LOG.setLevel(logging.INFO)


def log_section_banner(logger: logging.LoggerAdapter, section_title: str) -> None:
    TITLE_META = OJDExtraInfo(openjd_log_metadata=LogMetadata(LogPurpose.TITLE))
    logger.info("", extra=TITLE_META)
    logger.info("==============================================", extra=TITLE_META)
    logger.info(f"--------- {section_title}", extra=TITLE_META)
    logger.info(
        "==============================================",
        extra=TITLE_META,
    )


def log_subsection_banner(logger: logging.LoggerAdapter, section_title: str) -> None:
    TITLE_META = OJDExtraInfo(openjd_log_metadata=LogMetadata(LogPurpose.TITLE))
    logger.info(
        "----------------------------------------------",
        extra=TITLE_META,
    )
    logger.info(section_title, extra=TITLE_META)
    logger.info(
        "----------------------------------------------",
        extra=TITLE_META,
    )
