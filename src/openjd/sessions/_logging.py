# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import logging

__all__ = "LOG"

# Name the logger for the sessions module, rather than this specific file
LOG = logging.getLogger(".".join(__name__.split(".")[:-1]))
LOG.setLevel(logging.INFO)
