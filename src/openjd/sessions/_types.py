# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from enum import Enum

from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import EmbeddedFiles as EmbeddedFiles_2023_09
from openjd.model.v2023_09 import EmbeddedFileText as EmbeddedFileText_2023_09
from openjd.model.v2023_09 import Environment as Environment_2023_09
from openjd.model.v2023_09 import EnvironmentScript as EnvironmentScript_2023_09
from openjd.model.v2023_09 import StepScript as StepScript_2023_09

# ---- Types to export

EnvironmentIdentifier = str

# Turn this into a Union as new schemas are added.
StepScriptModel = StepScript_2023_09

# Turn this into a Union as new schemas are added.
EnvironmentModel = Environment_2023_09
EnvironmentScriptModel = EnvironmentScript_2023_09


class ActionState(str, Enum):
    RUNNING = "running"
    """The action is actively running."""

    CANCELED = "canceled"
    """The action has been canceled and is no longer running."""

    TIMEOUT = "timeout"
    """The action has been canceled due to reaching its runtime limit."""

    FAILED = "failed"
    """The action is no longer running, and exited with a non-zero
    return code."""

    SUCCESS = "success"
    """The action is no longer running, and exited with a zero
    return code."""


# --- Internal types

# dev note: WHen adding support for new schemas, or when an existing schema adds a
# new kind of embedded file, then make these a Union of all of the relevant types.
EmbeddedFileType = EmbeddedFileText_2023_09
EmbeddedFilesListType = EmbeddedFiles_2023_09

# Turn this into a Union as new schemas are added.
ActionModel = Action_2023_09
