# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Any, Callable, Optional

from openjd.model import RevisionExtensions, SpecificationRevision

from ._logging import LOG, LogContent, LogExtraInfo

__all__ = ("ActionMessageKind", "ActionMonitoringFilter", "redact_openjd_redacted_env_requests")


def redact_openjd_redacted_env_requests(command_str: str) -> str:
    """Redact sensitive information in command strings before they're processed by the regular redaction mechanism.

    For example, if an openjd session is about to run the following command as a subprocess:

    python -c "print('openjd_redacted_env: SECRETKEY=SECRETVAL')"

    Once that print statement is received by the logger filter it will become redacted, but if we
    were to log the full line before executing it in the subprocess, it would be unredacted.

    This method will turn:

    python -c "print('openjd_redacted_env: SECRETKEY=SECRETVAL')"

    to

    python -c "print('openjd_redacted_env: ********

    So it may be safely logged.

    Args:
        command_str: The command string that might contain sensitive information

    Returns:
        The command string with sensitive information redacted
    """
    # Find the position of the redaction token
    token = "openjd_redacted_env:"
    pos = command_str.find(token)

    # Fast path for the common case where there's no redaction needed
    if pos == -1:
        return command_str

    # If this is a redacted env command, redact everything after the token
    return command_str[: pos + len(token)] + " ********"


class ActionMessageKind(Enum):
    PROGRESS = "progress"  # A progress percentile for the running action
    STATUS = "status"  # A status message
    FAIL = "fail"  # A failure message
    ENV = "env"  # Defining an environment variable
    REDACTED_ENV = "redacted_env"  # Defining an environment variable with redacted value in logs
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

openjd_env_actions_filter_regex = "^(openjd_env|openjd_redacted_env|openjd_unset_env)"
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
        revision_extensions: Optional[RevisionExtensions] = None,
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
            revision_extensions (Optional[RevisionExtensions]): Contains information about the
                specification revision and supported extensions.
        """
        super().__init__(name)
        self._session_id = session_id
        self._callback = callback
        self._suppress_filtered = suppress_filtered
        self._revision_extensions = revision_extensions

        # Initialize set to store sensitive values for redaction
        self._redacted_values: set[str] = set()
        # Initialize set to store line-specific redactions (for multi-line secrets)
        self._redacted_lines: set[str] = set()
        self._internal_handlers = {
            ActionMessageKind.PROGRESS: self._handle_progress,
            ActionMessageKind.STATUS: self._handle_status,
            ActionMessageKind.FAIL: self._handle_fail,
            ActionMessageKind.ENV: self._handle_env,
            ActionMessageKind.REDACTED_ENV: self._handle_redacted_env,
            ActionMessageKind.UNSET_ENV: self._handle_unset_env,
            ActionMessageKind.SESSION_RUNTIME_LOGLEVEL: self._handle_session_runtime_loglevel,
        }

    def _redactions_enabled(self) -> bool:
        """Check if redacted environment variables are enabled.

        Redactions are enabled if either:
        1. The specification revision is newer than v2023_09, OR
        2. The REDACTED_ENV_VARS extension is explicitly enabled

        Returns:
            bool: True if redactions are enabled, False otherwise.
        """
        return self._revision_extensions is not None and (
            self._revision_extensions.spec_rev > SpecificationRevision.v2023_09
            or "REDACTED_ENV_VARS" in self._revision_extensions.extensions
        )

    def apply_message_redaction(self, record: logging.LogRecord):
        """Redact the log message if it contains any substrings which have been registered for redaction

        Args:
            record (logging.LogRecord): The log record to check.
        """
        # Check if we need to redact any sensitive values from the log message
        if (self._redacted_values or self._redacted_lines) and isinstance(record.msg, str):

            # If we have args, first do string formatting, then redact
            try:
                record.msg = record.msg % record.args
                record.args = ()  # Clear args since we've done the formatting
            except Exception:
                # If string formatting fails, fall back to just redacting the message
                LOG.warning(
                    "Failed to format log message for redaction. Proceeding with redaction on unformatted message."
                )

            # Check if the entire message matches a line in the redacted_lines set
            if record.msg in self._redacted_lines:
                record.msg = "*" * 8
                record.args = ()
                return True

            # Find all segments that need redaction
            segments_to_redact = []
            for value in self._redacted_values:
                if value:
                    start = 0
                    while True:
                        pos = record.msg.find(value, start)
                        if pos == -1:
                            break
                        segments_to_redact.append((pos, pos + len(value)))
                        start = pos + 1

            # If we found segments to redact, merge overlapping segments
            if segments_to_redact:
                # Sort segments by start position
                segments_to_redact.sort()

                # Merge overlapping segments
                merged_segments = []
                current_start, current_end = segments_to_redact[0]

                for start, end in segments_to_redact[1:]:
                    if start <= current_end:
                        # Segments overlap, extend current segment
                        current_end = max(current_end, end)
                    else:
                        # No overlap, add current segment and start new one
                        merged_segments.append((current_start, current_end))
                        current_start, current_end = start, end

                # Add the last segment
                merged_segments.append((current_start, current_end))

                # Apply redactions from end to start to avoid position shifts
                msg_chars = list(record.msg)
                for start, end in reversed(merged_segments):
                    msg_chars[start:end] = list("*" * 8)  # Always use 8 asterisks for redaction
                record.msg = "".join(msg_chars)
                record.args = ()

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
        try:

            if getattr(record, "session_id", None) != self._session_id:
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
                matched_named_groups = tuple(
                    k for k, v in match.groupdict().items() if v is not None
                )
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

                # Check if this is a redacted_env message and the extension is not enabled
                if (
                    message_kind == ActionMessageKind.REDACTED_ENV
                    and not self._redactions_enabled()
                ):
                    LOG.warning(
                        "Received openjd_redacted_env message but REDACTED_ENV_VARS extension is not enabled",
                        extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT),
                    )
                    # We still process the message - just log the warning

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
        finally:
            # Always check for redaction before returning
            self.apply_message_redaction(record)

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

    def _parse_env_variable(self, message: str) -> tuple[str, str, bool, int, Optional[str]]:
        """Parse an environment variable assignment string.

        Args:
            message (str): The message containing the variable assignment

        Returns:
            tuple: (variable_name, variable_value, is_valid, equals_position, original_value)
            where equals_position is the index of the equals sign in the original message
            and original_value is the value before JSON parsing (if applicable)

        A correctly formed message is of the form:
        <varname>=<value>
        where:
          <varname> consists of latin alphanumeric characters and the underscore,
                   and starts with a non-digit
          <value> can be any characters including empty.
        """
        message = message.lstrip()

        # Find the position of the equals sign
        equals_position = message.find("=")
        if equals_position == -1:
            return "", "", False, -1, None

        # Check if the message is valid
        is_valid = envvar_set_matcher_str.match(message) or envvar_set_matcher_json.match(message)

        if not is_valid:
            return "", "", False, equals_position, None

        # Parse the variable name and value
        try:
            original_value = None
            if envvar_set_matcher_str.match(message):
                name, _, value = message.partition("=")
            else:
                # Handle JSON format
                try:
                    # Store the original value before JSON parsing
                    original_value = message[equals_position + 1 :]
                    message_json_str = json.loads(message)
                    name, _, value = message_json_str.partition("=")
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"Unterminated string starting at: line {e.lineno} column {e.colno} (char {e.pos})"
                    )
            return name, value, True, equals_position, original_value
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Unterminated string starting at: line {e.lineno} column {e.colno} (char {e.pos})"
            )

    def _handle_env_error(self, error_message: str, is_redacted: bool = False) -> None:
        """Handle errors in environment variable processing.

        Args:
            error_message (str): The error message
            is_redacted (bool): Whether this is for a redacted env var
        """
        if is_redacted and self._redactions_enabled():
            LOG.warning(
                f"Malformed openjd_redacted_env command: {error_message} No environment variable will be set.",
                extra=LogExtraInfo(openjd_log_content=LogContent.COMMAND_OUTPUT),
            )
        else:
            # Callback to fail and cancel action on this error
            self._callback(ActionMessageKind.ENV, error_message, True)

        raise ValueError(error_message)

    def _handle_env(self, message: str) -> None:
        """Local handling of the Env messages.

        Args:
            message (str): The message after the leading 'openjd_env: ' prefix
        """
        name, value, is_valid, _, _ = self._parse_env_variable(message)

        if not is_valid:
            self._handle_env_error("Failed to parse environment variable assignment.")

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

    def _handle_redacted_env(self, message: str) -> None:
        """Local handling of the Redacted Env messages. Similar to _handle_env but
        redacts the value in logs and adds it to the set of values to redact in future logs.

        Args:
            message (str): The message after the leading 'openjd_redacted_env: ' prefix
        """

        message = message.lstrip()

        # Use the shared parsing logic to validate and extract the variable
        name, value, is_valid, equals_position, original_value = self._parse_env_variable(message)

        # Determine the value to redact
        redaction_value = None
        if is_valid:
            # Use the properly parsed value for redaction
            redaction_value = value

            # If we have an original value (from JSON parsing), add it to redaction set too
            if original_value is not None:
                self._redacted_values.add(original_value)
        elif equals_position != -1:  # Invalid format but has equals sign
            # Fall back to extracting value directly from the message
            redaction_value = message[equals_position + 1 :]
        else:
            # No equals sign, use entire content
            redaction_value = message

        # Add the value to the redaction set
        if redaction_value:
            self._redacted_values.add(redaction_value)

            # Add the individual parts if we've received a string with newlines
            parts = redaction_value.split("\n")
            for i, part in enumerate(parts):
                if part:  # Skip empty parts
                    if i == 0 or i == len(parts) - 1:
                        # First and last parts go in the regular redaction set
                        self._redacted_values.add(part)
                    else:
                        # Middle parts go in the line redaction set
                        self._redacted_lines.add(part)

        # Handle invalid format
        if not is_valid:
            if self._redactions_enabled():
                if "=" not in message:
                    self._handle_env_error(
                        "Malformed openjd_redacted_env command: missing equals sign.",
                        is_redacted=True,
                    )
                else:
                    self._handle_env_error(
                        "Malformed openjd_redacted_env command: invalid format.", is_redacted=True
                    )
            else:
                err_message = "Failed to parse environment variable assignment."
                self._callback(ActionMessageKind.ENV, err_message, True)
            return

        # Only set the environment variable if the extension is enabled
        if self._redactions_enabled():
            self._callback(ActionMessageKind.ENV, {"name": name, "value": value}, False)
