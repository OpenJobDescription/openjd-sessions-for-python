# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Any, Callable

from ._logging import LOG, LogContent, LogExtraInfo

__all__ = ("ActionMessageKind", "ActionMonitoringFilter")


class ActionMessageKind(Enum):
    PROGRESS = "progress"  # A progress percentile for the running action
    STATUS = "status"  # A status message
    FAIL = "fail"  # A failure message
    ENV = "env"  # Defining an environment variable
    UNSET_ENV = "unset_env"  # Unsetting an environment variable

    # The following are not in the spec, but are utility provided by this runtime.
    SESSION_RUNTIME_LOGLEVEL = "session_runtime_loglevel"  # Setting the log level of this runtime


# A composite regex that matches one of the message kinds to a named capture group
# with the same name as the message kind.
filter_regex = (
    "^openjd_(?:"
    f"{'|'.join(f'(?P<{re.escape(v.value)}>{re.escape(v.value)})' for v in ActionMessageKind)}"
    "): (.+)$"
)
filter_matcher = re.compile(filter_regex)

openjd_env_actions_filter_regex = "^(openjd_env|openjd_unset_env)"
openjd_env_actions_filter_matcher = re.compile(openjd_env_actions_filter_regex)

# A regex for matching the assignment of a value to an environment variable
envvar_set_regex_str = "^[A-Za-z_][A-Za-z0-9_]*" "=" ".*$"  # Variable name
envvar_set_regex_json = '^(")?[A-Za-z_][A-Za-z0-9_]*' "=" ".*$"  # Variable name
envvar_set_matcher_str = re.compile(envvar_set_regex_str)
envvar_set_matcher_json = re.compile(envvar_set_regex_json)
envvar_unset_regex = "^[A-Za-z_][A-Za-z0-9_]*$"
envvar_unset_matcher = re.compile(envvar_unset_regex)


# This is a reworking/merging of TaskStatusFilter and FailureFilter
class ActionMonitoringFilter(logging.Filter):
    """Captures any Open Job Description-defined updates from the subprocess that are communicated
    in the form of single lines in stdout of the form:
    openjd_progress: <progress in the form of a float between 0.0 and 100.0>
    openjd_status: <string indicating the new status>
    openjd_fail: <string indicating a failure message>
    openjd_env: <env var name>=<string value>
    openjd_unset_env: <env var name>
    openjd_session_runtime_loglevel: [ERROR | WARNING | INFO | DEBUG]

    When such a message is detected in the log stream a given callback will be
    called with the details of the update message. The callback will be called
    with arguments:
        callback(ActionMessageKind.PROGRESS, <float between 0.0 and 100.0>)
        callback(ActionMessageKind.STATUS, <string indicating the new status>)
        callback(ActionMessageKind.FAIL, <string indicating a failure message>)
        callback(ActionMessageKind.ENV, {"name": <envvar name>, "value": <envvar value>})
        callback(ActionMessageKind.UNSET_ENV, <string indicating the name of the env var>)
        callback(ActionMessageKind.RUNTIME_LOGLEVEL, <integer log level>)
    """

    _session_id: str
    """The id that we're looking for in LogRecords.
    We only process records with the "session_id" attribute set to this value.
    """

    _callback: Callable[[ActionMessageKind, Any, bool], None]
    """Callback to invoke when one of the Open Job Description update messages is detected.
    Args:
        [0]: The kind of the update message.
        [1]: The information/message given after the Open Job Description message prefix ("openjd_<name>: ")
        [2]: A boolean to express whether or not the corresponding Action is to be Canceled & marked Failed
    """

    _suppress_filtered: bool
    """If true, then any Open Job Description output stream messages are removed from the log
    when filtering."""

    _internal_handlers: dict[ActionMessageKind, Callable[[str], None]]
    """A mapping from message kind to the specfic ActionMonitoringFilter method that
    will handle processing the message type."""

    # The range of allowable values for progress reporting
    _MIN_PROGRESS: float = 0.0
    _MAX_PROGRESS: float = 100.0

    def __init__(
        self,
        name: str = "",
        *,
        session_id: str,
        callback: Callable[[ActionMessageKind, Any, bool], None],
        suppress_filtered: bool = False,
    ):
        """
        Args:
            name (str, optional): If name is specified, it names a logger which, together
                with its children, will have its events allowed through the filter. If name
                is the empty string, allows every event. Defaults to "".
            session_id (str): The id that we're looking for in LogRecords.
                We only process records with the "session_id" attribute set to this value.
            callback (Callable[[ActionMessageKind,Any], None]): A callable to call
                with a message payload when an Open Job Description message is found in the log.
            suppress_filtered (bool, optional): If True, then all Open Job Description messages
                will be filtered out of the log. Defaults to True.
        """
        super().__init__(name)
        self._session_id = session_id
        self._callback = callback
        self._suppress_filtered = suppress_filtered
        self._internal_handlers = {
            ActionMessageKind.PROGRESS: self._handle_progress,
            ActionMessageKind.STATUS: self._handle_status,
            ActionMessageKind.FAIL: self._handle_fail,
            ActionMessageKind.ENV: self._handle_env,
            ActionMessageKind.UNSET_ENV: self._handle_unset_env,
            ActionMessageKind.SESSION_RUNTIME_LOGLEVEL: self._handle_session_runtime_loglevel,
        }

    def filter(self, record: logging.LogRecord) -> bool:
        """Called automatically by Python's logging subsystem when a log record
        is sent to a log to which this filter class is applied.

        If the LogRecord does not have a 'session_id' attribute, or if the value of
        the attribute differs from this filter's session_id then the filter does nothing.

        Args:
            record (logging.LogRecord): Log record that was sent to the log.

        Returns:
            bool: If true then the Python logger will keep the record in the log,
                  else it will remove it.
        """
        if not hasattr(record, "session_id") or getattr(record, "session_id") != self._session_id:
            # Not a record for us to process
            return True
        if not isinstance(record.msg, str):
            # If something sends a non-string to the logger (e.g. via logger.exception) then
            # don't try to string match it.
            return True
        match = filter_matcher.match(record.msg)
        if match and match.lastindex is not None:
            message = match.group(match.lastindex)
            # Note: keys of match.groupdict() are the names of named groups in the regex
            matched_named_groups = tuple(k for k, v in match.groupdict().items() if v is not None)
            if len(matched_named_groups) > 1:
                # The only way that this happens is if filter_matcher is constructed incorrectly.
                all_matched_groups = ",".join(k for k in matched_named_groups)
                LOG.error(
                    f"Open Job Description: Malformed output stream filter matched multiple kinds ({all_matched_groups})",
                    extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT),
                )
                return True
            message_kind = ActionMessageKind(matched_named_groups[0])
            try:
                handler = self._internal_handlers[message_kind]
            except KeyError:
                LOG.error(
                    f"Open Job Description: Unhandled message kind ({message_kind.value})",
                    extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT),
                )
                return True
            try:
                handler(message)
            except ValueError as e:
                record.msg = record.msg + f" -- ERROR: {str(e)}"
                # There was an error. Don't suppress the message from the log.
                return True
            return not self._suppress_filtered

        # Check for "almost" matching openjd_env and openjd_unset_env commands
        lower_case_trimmed_msg: str = record.msg.lstrip().lower()
        if openjd_env_actions_filter_matcher.match(lower_case_trimmed_msg):
            # There was a minor error like spaces or case in the env commands
            err_message = (
                f"Open Job Description: Incorrectly formatted openjd env command ({record.msg})"
            )
            record.msg = record.msg + f" -- ERROR: {err_message}"

            # Callback to cancel the action and mark it as FAILED
            self._callback(ActionMessageKind.FAIL, err_message, True)
            return True

        return True

    def _handle_progress(self, message: str) -> None:
        """Local handling of Progress messages. Processes the message and then
        calls the provided handler,

        Args:
            message (str): The message after the leading 'openjd_progress: ' prefix
        """

        try:
            progress = float(message)
            if not (self._MIN_PROGRESS <= progress <= self._MAX_PROGRESS):
                raise ValueError()
            self._callback(ActionMessageKind.PROGRESS, progress, False)
        except ValueError:
            raise ValueError(
                f"Progress must be a floating point value between {self._MIN_PROGRESS} and {self._MAX_PROGRESS}, inclusive."
            )

    def _handle_status(self, message: str) -> None:
        """Local handling of Status messages. Just passes the message directly to
        the callback.

        Args:
            message (str): The message after the leading 'openjd_status: ' prefix
        """
        self._callback(ActionMessageKind.STATUS, message, False)

    def _handle_fail(self, message: str) -> None:
        """Local handling of Fail messages. Just passes the message directly to
        the callback.

        Args:
            message (str): The message after the leading 'openjd_fail: ' prefix
        """
        self._callback(ActionMessageKind.FAIL, message, False)

    def _handle_env(self, message: str) -> None:
        """Local handling of the Env messages.

        Args:
            message (str): The message after the leading 'openjd_env: ' prefix
        """
        message = message.lstrip()
        # A correctly formed message is of the form:
        # <varname>=<value>
        # where:
        #   <varname> consists of latin alphanumeric characters and the underscore,
        #             and starts with a non-digit
        #   <value> can be any characters including empty.
        if not envvar_set_matcher_str.match(message) and not envvar_set_matcher_json.match(message):
            err_message = "Failed to parse environment variable assignment."
            # Callback to fail and cancel action on this error
            self._callback(ActionMessageKind.ENV, err_message, True)
            raise ValueError(err_message)
        elif envvar_set_matcher_str.match(message):
            name, _, value = message.partition("=")
        else:
            message_json_str = json.loads(message)
            name, _, value = message_json_str.partition("=")

        self._callback(ActionMessageKind.ENV, {"name": name, "value": value}, False)

    def _handle_unset_env(self, message: str) -> None:
        """Local handling of the unset env messages.

        Args:
            message (str): The message after the leading 'openjd_unset_env: ' prefix
        """
        message = message.lstrip()
        # A correctly formed message is of the form:
        # <varname>
        # where:
        #   <varname> consists of latin alphanumeric characters and the underscore,
        #             and starts with a non-digit
        if not envvar_unset_matcher.match(message):
            err_message = "Failed to parse environment variable name."
            # Callback to fail and cancel action on this error
            self._callback(ActionMessageKind.UNSET_ENV, err_message, True)
            raise ValueError(err_message)
        self._callback(ActionMessageKind.UNSET_ENV, message, False)

    def _handle_session_runtime_loglevel(self, message: str) -> None:
        """Local handling of the session runtime loglevel messages.

        Args:
            message (str): The message after the leading 'openjd_session_runtime_loglevel: ' prefix
        """
        message = message.upper().strip()
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
        }
        loglevel = levels.get(message, None)
        if loglevel is not None:
            self._callback(ActionMessageKind.SESSION_RUNTIME_LOGLEVEL, loglevel, False)
        else:
            raise ValueError(
                f"Unknown log level: {message}. Known values: {','.join(levels.keys())}"
            )
