# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import logging

__all__ = "LOG"

# Name the logger for the sessions module, rather than this specific file
LOG = logging.getLogger(".".join(__name__.split(".")[:-1]))
LOG.setLevel(logging.INFO)


def log_section_banner(logger: logging.LoggerAdapter, section_title: str) -> None:
    logger.info("")
    logger.info("==============================================")
    logger.info(f"--------- {section_title}")
    logger.info("==============================================")


def log_subsection_banner(logger: logging.LoggerAdapter, section_title: str) -> None:
    logger.info("----------------------------------------------")
    logger.info(section_title)
    logger.info("----------------------------------------------")
