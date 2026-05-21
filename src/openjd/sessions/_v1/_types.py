# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Re-export types from Rust bindings and model.
from openjd._openjd_rs import ActionState
from openjd.model._v1.job import (
    Action as Action_2023_09,
    EmbeddedFile as EmbeddedFileText_2023_09,
    Environment as Environment_2023_09,
    EnvironmentScript as EnvironmentScript_2023_09,
    StepScript as StepScript_2023_09,
)

EnvironmentIdentifier = str

StepScriptModel = StepScript_2023_09
EnvironmentModel = Environment_2023_09
EnvironmentScriptModel = EnvironmentScript_2023_09

# Internal types
EmbeddedFileType = EmbeddedFileText_2023_09
EmbeddedFilesListType = list
ActionModel = Action_2023_09

__all__ = (
    "ActionModel",
    "ActionState",
    "EmbeddedFileType",
    "EmbeddedFilesListType",
    "EnvironmentIdentifier",
    "EnvironmentModel",
    "EnvironmentScriptModel",
    "StepScriptModel",
)
