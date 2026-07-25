# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import os
import re
import stat
import shlex
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Lock, Timer
from typing import Any, Callable, Optional, Sequence, Type, cast
from types import TracebackType
from tempfile import mkstemp

from openjd.model import SymbolTable
from openjd.model import FormatStringError
from openjd.model import evaluate_let_bindings

# The EXPR engine's typed value. Imported concretely (rather than duck-typed
# with getattr) so that a model API change fails loudly at import time instead
# of silently mis-classifying every optional integer field as "omitted".
# openjd.expr ships in the same distribution as openjd.model, which this module
# already hard-imports unreleased API from.
from openjd.expr import ExprValue, TypeCode
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import CancelationMethodDeferred as CancelationMethodDeferred_2023_09
from openjd.model.v2023_09 import CancelationMode as CancelationMode_2023_09
from ._embedded_files import EmbeddedFiles, EmbeddedFilesScope, _FileRecord, write_file_for_user
from ._logging import log_subsection_banner, LoggerAdapter, LogContent, LogExtraInfo
from ._os_checker import is_posix
from ._session_user import SessionUser
from ._subprocess import LoggingSubprocess
from ._types import ActionModel, ActionState, EmbeddedFilesListType
from ._win32._locate_executable import locate_windows_executable

__all__ = (
    "ScriptRunnerState",
    "CancelMethod",
    "TerminateCancelMethod",
    "NotifyCancelMethod",
    "ScriptRunnerBase",
    "apply_let_bindings",
    "resolve_action_arg_values",
    "resolve_effective_cancelation",
    "resolve_optional_int_field",
    "MAX_INT_FIELD_VALUE",
    "MAX_SCHEDULABLE_TIMEOUT_SECONDS",
)


_STRICT_INT_RE = re.compile(r"[+-]?[0-9]+")
"""The exact integer grammar accepted for dynamically resolved integer fields
(FEATURE_BUNDLE_1 ``timeout`` / ``notifyPeriodInSeconds`` format strings):
an optional sign followed by ASCII digits, exactly what Rust's ``str::parse``
accepts. See resolve_optional_int_field."""

MAX_INT_FIELD_VALUE = 2**63 - 1
"""Largest value accepted for a dynamically resolved integer field.

Three limits coincide here. openjd-rs's model rejects a literal ``timeout``
above ``i64::MAX`` at parse time; its runtime parses a resolved one with
``str::parse::<u64>()`` (``runner/mod.rs`` resolve_action_timeout), which *fails*
rather than saturating; and the EXPR engine's integers are ``i64``, so a larger
value cannot round-trip through ``WrappedAction.Timeout`` (RFC 0008) at all.
Python's model has no upper bound, so the bound is applied here — to literal and
resolved values alike, which is what keeps a forwarded value behaving exactly as
it would have unwrapped."""

MAX_SCHEDULABLE_TIMEOUT_SECONDS = 2**62 // 10**9
"""Largest action timeout we can actually enforce, in seconds (~146 years).

Two distinct limits sit above this. :class:`datetime.timedelta` tops out at
999,999,999 days and raises ``OverflowError`` on construction. Separately,
:class:`threading.Timer` computes an absolute deadline in CPython's internal
64-bit nanosecond time representation, which overflows just above 2**63
nanoseconds (measured: 9,223,372,036 s schedules, 9,223,372,037 s does not) —
that one raises on the timer's own thread, so the timeout silently never fires
rather than failing loudly. The bound here is deliberately well inside that
boundary rather than pinned to it, because the overflow is reported against the
platform's ``time_t`` and the exact threshold is not portable.

A timeout beyond this is indistinguishable from "no timeout", which is what
openjd-rs's ``Duration``-based timer effectively provides at that magnitude, so
the action runs unbounded instead of failing. See _timeout_from_seconds."""


def _over_range_message(description: str, value: int) -> str:
    """Error text for a value that is a valid integer but too large to use.

    Distinct from the bounds message, because "must be a positive integer" reads
    as nonsense for a value that plainly is one -- the problem is magnitude.
    """
    return f"{description} must be at most {MAX_INT_FIELD_VALUE}, got '{value}'"


def _timeout_from_seconds(seconds: int, logger: LoggerAdapter) -> Optional[timedelta]:
    """Convert a resolved timeout in seconds into an enforceable time limit.

    Returns ``None`` — no time limit — for a value too large to schedule (see
    :data:`MAX_SCHEDULABLE_TIMEOUT_SECONDS`), so that an absurd-but-valid
    timeout runs the action to completion the way openjd-rs does, rather than
    raising ``OverflowError`` out of the public Session API.
    """
    if seconds > MAX_SCHEDULABLE_TIMEOUT_SECONDS:
        logger.warning(
            f"Action timeout of {seconds} seconds is larger than this runtime can enforce; "
            "the action will run without a time limit.",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        return None
    return timedelta(seconds=seconds)


class ScriptRunnerState(str, Enum):
    """State of a ScriptRunner."""

    READY = "ready"
    """Runner is not currently running anything, and can run an Action.
    """

    RUNNING = "running"
    """Runner is actively running an Action.
    """

    CANCELING = "canceling"
    """Runner is actively in the act of canceling a running Action.
    """

    CANCELED = "canceled"
    """The Action that was run by the runner was canceled.
    """

    TIMEOUT = "timeout"
    """The action has been canceled due to reaching its runtime limit."""

    FAILED = "failed"
    """Runner is done running the subprocess, and the subprocess failed.
    """

    SUCCESS = "success"
    """Runner is done running the subprocess, and the subprocess returned success.
    """


class CancelMethod:
    pass


@dataclass(frozen=True)
class TerminateCancelMethod(CancelMethod):
    """Immediately terminate the running subprocess via SIGKILL"""

    pass


TIME_FORMAT_STR = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class NotifyCancelMethod(CancelMethod):
    """Cancelation via "notify then terminate". First send a SIGTERM,
    then wait for a delay, then send a SIGKILL if the process is still running.
    """

    terminate_delay: timedelta
    """Amount of time after a SIGTERM to wait to do the SIGKILL"""


def resolve_action_arg_values(args: Optional[Sequence], symtab: SymbolTable) -> list[str]:
    """Resolve an action's ``args`` field into its flat list of argument
    strings (excluding the command).

    RFC 0005 §1.3.2 argument semantics, mirroring openjd-rs's
    resolve_action_args: a whole-field expression argument resolves typed —
    a null result skips the argument, a list result flattens inline (one
    argument per element, rendered with the engine's display coercion), and
    a scalar becomes a single argument. Multi-segment format strings and
    legacy (non-EXPR) expressions resolve to their string form.

    Shared by the enforcement path (:meth:`ScriptRunnerBase._run_action`)
    and the RFC 0008 ``WrappedAction.Args`` injection, so a wrap hook sees
    exactly the arguments the wrapped action would have run with unwrapped.

    Raises:
        FormatStringError: If an argument's expression cannot be resolved.
    """
    resolved: list[str] = []
    if args is not None:
        for arg in args:
            try:
                value = arg.resolve_value(symtab=symtab)
            except FormatStringError:
                # Mirror openjd-rs's resolve_action_args: when typed
                # resolution fails (e.g. a legacy-parsed expression meeting
                # a typed symbol value), fall back to plain string
                # resolution — which raises FormatStringError itself if the
                # argument is genuinely unresolvable.
                resolved.append(arg.resolve(symtab=symtab))
                continue
            if isinstance(value, str):
                resolved.append(value)
            elif value.is_null:
                continue
            elif value.type.type_code == TypeCode.LIST:
                resolved.extend(str(element) for element in value)
            else:
                resolved.append(str(value))
    return resolved


def resolve_optional_int_field(
    value: Any,
    symtab: SymbolTable,
    *,
    ge: Optional[int] = None,
    le: Optional[int] = None,
    description: str,
) -> Optional[int]:
    """Resolve an optional int-or-format-string field (e.g. an action's
    ``timeout`` or a cancelation's ``notifyPeriodInSeconds``) into an
    optional integer.

    - ``None`` (field omitted) stays ``None``.
    - A literal ``int`` passes through unchecked: literal values were
      bounds-checked by the static validator at parse time.
    - A FormatString (FEATURE_BUNDLE_1) is resolved against ``symtab``
      using typed resolution. A whole-field expression that resolves to a
      typed null is treated as if the field were not provided (``None`` —
      the caller applies any positional schema default). Any other result
      — including a genuine empty string — must be an integer within the
      given bounds; the bounds apply here because format-string values
      could not be checked at parse time. This matches the openjd-rs
      runtime (resolve_action_timeout / resolve_notify_period_seconds),
      which only treats an ExprValue::Null result as "field omitted" and
      errors on an empty string.

    Raises:
        ValueError: If the resolved value is not an integer, or violates
            the ``ge``/``le`` bounds.
        FormatStringError: If expression resolution itself fails.
    """
    if value is None:
        return None
    if ge is not None and le is not None:
        constraint = f"between {ge} and {le}"
    elif ge == 1 and le is None:
        constraint = "a positive integer"
    elif ge is not None:
        constraint = f"an integer >= {ge}"
    else:
        constraint = "an integer"
    if isinstance(value, int):
        # Literal values were bounds-checked by the static validator at parse
        # time -- with one exception: the validator has no upper bound. Reject an
        # over-range literal here, mirroring openjd-rs, whose model rejects it at
        # parse time and whose runtime's str::parse fails rather than saturating.
        if value > MAX_INT_FIELD_VALUE:
            raise ValueError(_over_range_message(description, value))
        return value
    # Typed resolution: a whole-field EXPR expression yields the engine's
    # typed value, so a null result is distinguishable from a genuine empty
    # string. Multi-segment and legacy (non-EXPR) format strings fall back
    # to plain string resolution — correct, since typed nulls only exist
    # under EXPR whole-field semantics (Template Schemas 5.3).
    resolved_value = value.resolve_value(symtab=symtab)
    if isinstance(resolved_value, ExprValue) and resolved_value.is_null:
        return None
    resolved = str(resolved_value)
    # Strict ASCII integer grammar, matching the Rust runtime's str::parse
    # (openjd-rs resolve_action_timeout / resolve_notify_period_seconds).
    # Python's int() is more lenient — it accepts surrounding whitespace,
    # digit-group underscores ("1_0" == 10), and non-ASCII decimal digits —
    # all of which Rust rejects, so accepting them here would be a
    # spec-observable divergence.
    if _STRICT_INT_RE.fullmatch(resolved) is None:
        raise ValueError(f"{description} must be {constraint}, got '{resolved}'")
    result = int(resolved)
    if result > MAX_INT_FIELD_VALUE:
        # See MAX_INT_FIELD_VALUE: openjd-rs's str::parse fails rather than
        # saturating, so an over-range value must fail here too rather than
        # reach the timer.
        raise ValueError(_over_range_message(description, result))
    if (ge is not None and result < ge) or (le is not None and result > le):
        raise ValueError(f"{description} must be {constraint}, got '{result}'")
    return result


def resolve_effective_cancelation(
    cancelation: Any, symtab: SymbolTable
) -> tuple[Optional[str], Optional[int]]:
    """Resolve an action's cancelation config against the symbol table into
    an effective ``(mode, notify_period_seconds)`` pair.

    What is the problem this solves?

    Format strings are normally delay-processed: the parser stores "this is
    a format string" and the value resolves right before the action runs.
    But a cancelation's ``mode`` is the *schema selector* — the parser needs
    it to know which object shape it is reading — while a forwarded value
    like ``mode: "{{WrappedAction.Cancelation.Mode}}"`` (RFC 0008
    round-trip forwarding) only exists at run time. The model therefore
    carries such a mode as :class:`CancelationMethodDeferred`, and *this*
    function is where the deferred decision finally lands: with the
    ``WrappedAction.*`` values in the symbol table, the mode expression
    resolves to ``"TERMINATE"``, ``"NOTIFY_THEN_TERMINATE"``, or null — and
    a null mode means the whole cancelation object is treated as never
    declared (the runtime default applies).

    Returns:
        (mode, notify_period_seconds) where ``mode`` is ``"TERMINATE"``,
        ``"NOTIFY_THEN_TERMINATE"``, or ``None`` (no ``<Cancelation>``
        declared, or a deferred mode that resolved to null); and
        ``notify_period_seconds`` is ``None`` when the field was omitted or
        its whole-field expression resolved to null — the caller applies
        the positional schema default.

    Raises:
        ValueError: If a deferred mode resolves to anything other than the
            two method names or null, if a resolved TERMINATE carries a
            non-null notify period, or if a notify period does not resolve
            to a positive integer.
        FormatStringError: If expression resolution itself fails.
    """

    def resolve_period(period: Any) -> Optional[int]:
        # Bounds mirror the static validator's on literal values (Template
        # Schemas 5.3.2: 1..600); see resolve_optional_int_field.
        return resolve_optional_int_field(
            period, symtab, ge=1, le=600, description="notifyPeriodInSeconds"
        )

    if cancelation is None:
        return (None, None)
    if isinstance(cancelation, CancelationMethodDeferred_2023_09):
        # Typed resolution. Null semantics apply only to a whole-field
        # expression ("{{ ... }}" with no surrounding text, target type
        # string? — Template Schemas 5.3), and resolve_value only yields a
        # typed null for a whole-field EXPR expression; every other format
        # string resolves to its plain string form. A format string that
        # happens to resolve to the empty string is NOT null; it falls
        # through to the "must resolve to..." error below (matching the
        # openjd-rs runtime, which errors on any non-null, non-mode-name
        # result).
        mode_value = cancelation.mode.resolve_value(symtab=symtab)
        if isinstance(mode_value, ExprValue) and mode_value.is_null:
            # Null mode drops the ENTIRE cancelation object: mode is the
            # object's required discriminator, so an "omitted" mode cannot
            # leave a partial object behind. The action behaves exactly as
            # if no <Cancelation> were declared.
            return (None, None)
        mode = str(mode_value)
        if mode == CancelationMode_2023_09.TERMINATE.value:
            # Post-resolution the object must validate against the resolved
            # variant's shape: TERMINATE admits no notify period.
            if resolve_period(cancelation.notifyPeriodInSeconds) is not None:
                raise ValueError(
                    "cancelation mode resolved to TERMINATE, which does not "
                    "accept notifyPeriodInSeconds"
                )
            return (CancelationMode_2023_09.TERMINATE.value, None)
        if mode == CancelationMode_2023_09.NOTIFY_THEN_TERMINATE.value:
            return (
                CancelationMode_2023_09.NOTIFY_THEN_TERMINATE.value,
                resolve_period(cancelation.notifyPeriodInSeconds),
            )
        raise ValueError(
            "cancelation mode must resolve to TERMINATE, NOTIFY_THEN_TERMINATE, "
            f"or null; got '{mode}'"
        )
    if cancelation.mode == CancelationMode_2023_09.TERMINATE:
        return (CancelationMode_2023_09.TERMINATE.value, None)
    # Direct attribute access, not getattr with a default: the mode above has
    # already established this is a NOTIFY_THEN_TERMINATE object, which always
    # carries the field. A getattr default would silently substitute the
    # positional 30/120 s default for the author's period if the model ever
    # renamed it.
    return (
        CancelationMode_2023_09.NOTIFY_THEN_TERMINATE.value,
        resolve_period(cancelation.notifyPeriodInSeconds),
    )


def apply_let_bindings(*, symtab: SymbolTable, let_bindings: list[str]) -> None:
    """Evaluate EXPR ``let`` bindings (RFC 0005) and add them to ``symtab``.

    ``let_bindings`` is a script's ``let`` field: an ordered list of
    ``"name = expression"`` strings. Each RHS is an EXPR expression evaluated
    against the symbol table built so far (so later bindings can reference
    earlier ones), and the engine's typed result is stored under the bound
    name — a let-bound path stays a path for property access, and float
    rendering fidelity is preserved — matching the Rust runtime's natively
    typed symbol table.

    The runners evaluate bindings after embedded-file *path* allocation and
    before file *contents* are written, so a binding may reference
    ``Env.File.*``/``Task.File.*`` and a file's ``data`` may reference
    let-bound values (mirroring openjd-rs's runner ordering).

    Raises:
        ValueError (FormatStringError/ExpressionError): if a binding's
            expression cannot be evaluated.
    """
    # Single-sourced in openjd.model (parse-memoized; skips malformed
    # bindings; raises ValueError naming the failing binding).
    evaluate_let_bindings(symtab=symtab, let_bindings=let_bindings)


class ScriptRunnerBase(ABC):
    """Base class for a runnable Environment or Step Script.
    Responsible for running a *single* Action, and optionally canceling it.
    """

    _logger: LoggerAdapter
    """The logger to which all messages should be sent from this and the subprocess.
    """

    _user: Optional[SessionUser]
    """The user to run the subprocess as, if given.
    Else the subprocess is run as this process' user.
    """

    _os_env_vars: Optional[dict[str, Optional[str]]]
    """OS Environment variables and their values to inject into the running subprocess.
    """

    _session_working_directory: Path
    """The temporary directory in which the Session is running.
    """

    _startup_directory: Optional[Path]
    """cwd to set for the subprocess, if it's possible to set it.
    """

    _callback: Optional[Callable[[ActionState], None]]
    """Callback to invoke when the running subprocess has exited (or failed to start).
    """

    _process: Optional[LoggingSubprocess]
    """The subprocess that this runner is running, or has most recently run.
    """

    _run_future: Optional[Future]
    """The future within which the current Action for this runner is running.
    Will be None if no Action is running.
    """

    _cancel_gracetime_timer: Optional[Timer]
    """If not None, then this is a timer that is counting down the grace time
    for a NOTIFY_THEN_TERMINATE cancelation.
    self._on_notify_period_end() will be called when the timer expires.
    """

    _cancel_gracetime_end: Optional[datetime]
    """The time at which the gracetime of a NOTIFY_THEN_TERMINATE cancelation's
    graceperiod will expire.
    """

    _canceled: bool
    """True iff the subprocess was canceled.
    """

    _executable_not_found: bool
    """True only on Windows, and only if the executable command that we were given
    could not be found before even trying to run the subprocess.
    """

    _notify_canceled_action_as_failed: bool
    """True iff the subprocess was canceled but action needs to be notified as FAILED.
    """

    _pending_cancel: Optional[tuple[Optional[timedelta], bool]]
    """A cancel that arrived before the subprocess existed, as
    ``(time_limit, mark_action_failed)``.

    ``cancel()`` is a cross-thread API, so it can land during action setup —
    between resolving the action and the subprocess actually being created. The
    request is remembered here and applied by :meth:`_run` as soon as the
    subprocess exists, so a cancel is never silently dropped (openjd-rs holds
    the equivalent state in a sticky ``CancellationToken``).
    """

    _runtime_limit: Optional[Timer]
    """The Timer that will fire when the currently running Action has exhausted
    its runtime limit.
    Will be None if either no Action is running or the running Action has no time
    limit.
    """

    _runtime_limit_reached: bool
    """True if and only if the Action was terminated due to reaching its runtime limit."""

    _pool: ThreadPoolExecutor
    """Pool in which to run futures for this runner.
    """

    _lock: Lock
    """A lock that must be obtained prior to mutating/creating the subprocess
    running state of this runner.
    """

    _state_override: Optional[ScriptRunnerState]
    """An override for subclasses to use to indicate that the runner is in a specific state.
    e.g. We failed to write embedded files before even trying to run the action.
    """

    _resolved_cancel_method: Optional[CancelMethod]
    """The running action's effective cancel method, resolved by
    :meth:`_run_action` right before subprocess launch — against the same
    final (let/embedded-file enriched) symbol table the command and args
    resolved with — and consumed by the runners' :meth:`cancel`.

    Resolving at launch matches the openjd-rs runtime (``run_action``
    resolves ``cancel_method_for_action`` up front): a cancelation whose
    deferred mode or notify period cannot be resolved fails the action at
    start rather than surfacing only if a cancel later occurs, and the
    resolution scope is the action's own (a mode referencing a script-level
    ``let`` binding resolves correctly). ``None`` until an action launches.
    """

    def __init__(
        self,
        *,
        logger: LoggerAdapter,
        user: Optional[SessionUser] = None,
        # environment for the subprocess that is run
        os_env_vars: Optional[dict[str, Optional[str]]] = None,
        # The working directory of the session
        session_working_directory: Path,
        # `cwd` for the subprocess that's run
        startup_directory: Optional[Path] = None,
        # Callback to invoke when a running action exits
        callback: Optional[Callable[[ActionState], None]] = None,
    ):
        """
        Arguments:
            logger (Logger): The logger to which all messages should be sent from this and the
                subprocess.
            os_env_vars (dict[str, str]): Environment variables and their values to inject into the
                running subprocess.
            session_working_directory (Path): The temporary directory in which the Session is running.
            user (Optional[SessionUser]): The user to run the subprocess as, if given. Defaults to the
                current user.
            startup_directory (Optional[Path]): cwd to set for the subprocess, if it's possible to set it.
            callback (Optional[Callable[[ActionState], None]]): Callback to invoke when the running
                subprocess has started,  exited, or failed to start. Defaults to None.
        """

        self._logger = logger
        self._user = user
        self._os_env_vars = os_env_vars
        self._session_working_directory = session_working_directory
        self._startup_directory = startup_directory
        self._callback = callback

        self._process = None
        self._run_future = None
        self._cancel_gracetime_timer = None
        self._cancel_gracetime_end = None
        self._canceled = False
        self._notify_canceled_action_as_failed = False
        self._runtime_limit = None
        self._runtime_limit_reached = False
        self._executable_not_found = False
        self._lock = Lock()
        # Will run at most the run futures
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._state_override = None
        self._resolved_cancel_method = None
        self._pending_cancel = None
        self._print_section_banner = True

    # Context manager for use in our tests
    def __enter__(self) -> "ScriptRunnerBase":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        """Performs a clean shutdown on the runner. This shutsdown the internal
        ThreadPoolExectutor.
        """
        self._pool.shutdown()

    def _fail_action(self, message: str) -> None:
        """Fail the action through the normal failure path: surface the
        failure reason to the customer via the action filter
        (``openjd_fail``), set the FAILED state override, and invoke the
        callback. The subprocess's future may not have been started yet,
        but the Session still needs to know that the action is over.
        """
        self._logger.info(
            f"openjd_fail: {message}",
            extra=LogExtraInfo(openjd_log_content=LogContent.EXCEPTION_INFO),
        )
        self._state_override = ScriptRunnerState.FAILED
        if self._callback is not None:
            self._callback(ActionState.FAILED)

    @abstractmethod
    def cancel(
        self, *, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:  # pragma: nocover
        """Cancel the runner's running Action according to whatever method is dictated
        by the specific script being run.

        Arguments:
            time_limit (Optional[timedelta]): If provided, then the cancel must be
                completed within the given number of seconds. This is for urgent
                cancels (e.g. in response to the controlling process getting a SIGTERM).
                Note: a value of 0 turns a notify-then-terminate cancel into a terminate
        """
        raise NotImplementedError("Derived class must implement this.")

    @property
    def state(self) -> ScriptRunnerState:
        """Get the state of this runner."""
        if self._state_override is not None:
            return self._state_override
        if self._process is None:
            return ScriptRunnerState.READY
        # Check on the state of the future for done/canceled first
        # If the future is done, then we have a terminal state.
        assert self._run_future is not None  # For the type checker
        if self._run_future.done():
            if self._canceled and self._notify_canceled_action_as_failed:
                return ScriptRunnerState.FAILED
            if self._canceled and self._runtime_limit_reached:
                return ScriptRunnerState.TIMEOUT
            elif self._canceled:
                return ScriptRunnerState.CANCELED
            elif self._process.failed_to_start or self._process.exit_code != 0:
                return ScriptRunnerState.FAILED
            else:
                return ScriptRunnerState.SUCCESS
        # Future's still running, so we're CANCELING if we've been canceled
        # otherwise we're RUNNING.
        if self._canceled:
            return ScriptRunnerState.CANCELING
        # If the future's not done, then we're still running.
        return ScriptRunnerState.RUNNING

    @property
    def runtime_limit_reached(self) -> bool:
        return self._runtime_limit_reached

    @property
    def exit_code(self) -> Optional[int]:
        """Note: It *is* possible to fail without an exit code."""
        if self._process is not None:
            return self._process.exit_code
        return None

    def _run(self, args: Sequence[str], time_limit: Optional[timedelta] = None) -> None:
        with self._lock:
            if self.state != ScriptRunnerState.READY:
                raise RuntimeError("This cannot be used to run a second subprocess.")
            if is_posix():
                script = self._generate_command_shell_script(args)
                filehandle, filename = mkstemp(
                    dir=self._session_working_directory, suffix=".sh", text=True
                )
                os.close(filehandle)
                # Create the shell script, and make it runnable by the owner.
                # If user is defined, then this will make it owned by that user's group.
                write_file_for_user(
                    Path(filename),
                    script,
                    user=self._user,
                    additional_permissions=stat.S_IXUSR | stat.S_IXGRP,
                )
                self._logger.debug(
                    f"Wrote the following script to {filename}:\n{script}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.FILE_PATH | LogContent.FILE_CONTENTS
                    ),
                )
            else:
                try:
                    args = locate_windows_executable(
                        args, self._user, self._os_env_vars, str(self._session_working_directory)
                    )
                except RuntimeError as e:
                    self._fail_action(str(e))
                    return

            subprocess_args = [filename] if is_posix() else args
            self._process = LoggingSubprocess(
                logger=self._logger,
                args=subprocess_args,
                user=self._user,
                os_env_vars=self._os_env_vars,
                working_dir=str(self._session_working_directory),
            )

            if time_limit:
                self._runtime_limit = Timer(time_limit.total_seconds(), self._on_timelimit)
                self._runtime_limit.start()

            if self._print_section_banner:
                log_subsection_banner(self._logger, "Phase: Running action")
            self._run_future = self._pool.submit(self._process.run)
        # Intentionally leave the lock section. If the process was *really* fast,
        # then it's possible for the future to have finished before we get to add
        # the done-callback. That results in the done-callback being called from
        # *this* thread.
        self._run_future.add_done_callback(self._on_process_exit)

        # Block until the subprocess actually starts.
        # This will prevent race conditions where the user starts up the Action,
        # and then is erroneously told that the Action is done because the future
        # for _process.run hasn't actually gotten far enough to start the subprocess
        # before we check self.state
        self._process.wait_until_started()

        # A cancel that landed during setup, before there was a subprocess to
        # signal, is applied now rather than dropped. See _pending_cancel.
        pending = self._pending_cancel
        if pending is not None:
            self._pending_cancel = None
            if self._resolved_cancel_method is not None:
                self._cancel(self._resolved_cancel_method, *pending)

        if self.state == ScriptRunnerState.RUNNING and self._callback is not None:
            # Let the caller know that the process is running.
            self._callback(ActionState.RUNNING)

    def _generate_command_shell_script(self, args: Sequence[str]) -> str:
        """Generate a shell script for running a command given by the args."""
        script = list[str]()
        script.append("#!/bin/sh\n")
        if self._os_env_vars:
            for name, value in self._os_env_vars.items():
                if value is None:
                    script.append(f"unset {name}")
                else:
                    script.append(f"export {name}={shlex.quote(value)}")
        if self._startup_directory is not None:
            # Note: Single quotes around the path as it may have spaces, and we don't want to
            # process any shell commands in the path.
            script.append(f"cd '{self._startup_directory}'")
        script.append("exec " + shlex.join(args))
        return "\n".join(script)

    def _materialize_files(
        self,
        scope: EmbeddedFilesScope,
        files: EmbeddedFilesListType,
        dest_directory: Path,
        symtab: SymbolTable,
        let_bindings: Optional[list[str]] = None,
        preallocated_records: Optional[list[_FileRecord]] = None,
    ) -> None:
        """Helper for derived classes that wraps all of the logic around
        materializing embedded files to disk.

        When ``let_bindings`` is given, they are evaluated between file-path
        allocation and content writing (RFC 0005, mirroring the openjd-rs
        runners): a file's *path* never depends on ``let`` values (filenames
        are plain strings), so ``Env.File.*``/``Task.File.*`` are available to
        the bindings, while a file's ``data`` is written afterwards so it can
        reference let-bound values.

        When ``preallocated_records`` is given (RFC 0008: a wrap
        environment's files, whose paths the Session allocates once and
        reuses across wrap-hook invocations), path allocation is skipped;
        the records' symbols are defined in ``symtab`` and the contents are
        re-resolved and written as usual.

        Note on the per-invocation rewrite: the resolved bytes are in fact
        invariant across a wrap environment's hook invocations, because model
        validation rejects ``WrappedAction.*`` both in an environment script's
        ``data`` and in its ``let`` bindings, and every other symbol a wrap
        env's ``data`` may reference (``Param.*``, ``Session.*``, ``Job.Name``,
        the now-stable ``Env.File.*``) is fixed for the session. The rewrite is
        kept anyway, deliberately: it costs one small write immediately before
        spawning a subprocess — the same cost class as the spawn — and it makes
        each invocation deterministic even if a previous invocation's subprocess
        modified a ``runnable`` embedded script in place.
        """
        file_writer = EmbeddedFiles(
            logger=self._logger,
            scope=scope,
            session_files_directory=dest_directory,
            user=self._user,
        )
        try:
            if preallocated_records is not None:
                records = preallocated_records
                file_writer.register_file_paths(records, symtab)
            else:
                records = file_writer.allocate_file_paths(files, symtab)
            if let_bindings:
                apply_let_bindings(symtab=symtab, let_bindings=let_bindings)
            file_writer.write_file_contents(records, symtab)
        except (RuntimeError, ValueError) as exc:
            # Had a problem writing at least one file to disk, or evaluating
            # a `let` binding (FormatStringError/ExpressionError subclass
            # ValueError). Surface the error.
            self._fail_action(str(exc))

    def _apply_let_bindings_or_fail(self, symtab: SymbolTable, let_bindings: list[str]) -> bool:
        """Evaluate the script's EXPR ``let`` bindings into ``symtab``. On an
        evaluation error the action is failed through the normal failure path
        (openjd_fail log, FAILED state, callback). Returns True on success."""
        try:
            apply_let_bindings(symtab=symtab, let_bindings=let_bindings)
        except ValueError as exc:
            self._fail_action(str(exc))
            return False
        return True

    def _run_action(
        self,
        action: ActionModel,
        symtab: SymbolTable,
        *,
        default_timeout: Optional[timedelta] = None,
        default_notify_period_seconds: int = 30,
    ) -> None:
        """Helper for derived classes to run a specific Action.

        Args:
            action (ActionModel): The action model to be executed. Must be an
                instance of Action_2023_09.
            symtab (SymbolTable): Symbol table used for resolving command and
                arguments.
            default_timeout (Optional[timedelta], optional): Default timeout duration
                for the action if no timeout is specified in the action. The default behaviour if
                None is passed will allow the action to run indefinitely until it completes.
            default_notify_period_seconds (int): The Template Schemas 5.3.2
                positional default applied when a NOTIFY_THEN_TERMINATE
                cancelation omits its notify period (120 for a task's onRun,
                30 for any other action).
        """
        assert isinstance(action, Action_2023_09)
        try:
            command = [action.command.resolve(symtab=symtab)]
            # RFC 0005 §1.3.2 typed argument semantics (null skip, list
            # flattening) — see resolve_action_arg_values, shared with the
            # RFC 0008 WrappedAction.Args injection.
            command.extend(resolve_action_arg_values(action.args, symtab))
        except FormatStringError as exc:
            # Extremely unlikely since a JobTemplate needs to have passed
            # validation before we could be running it, but just to be safe.
            self._fail_action(str(exc))
        else:
            time_limit: Optional[timedelta] = default_timeout
            # A FormatString timeout (FEATURE_BUNDLE_1) is resolved right
            # before the action runs. A whole-field expression that
            # resolves to a typed null — e.g. forwarding
            # `timeout: "{{WrappedAction.Timeout}}"` (RFC 0008) when the
            # wrapped action specified no timeout — is treated as if the
            # field were not provided, so the positional default applies.
            try:
                seconds = resolve_optional_int_field(
                    action.timeout, symtab, ge=1, description="timeout"
                )
            except ValueError as exc:
                # FormatStringError (resolution failure) subclasses
                # ValueError, so this covers both a failed resolution and a
                # non-positive-integer resolved value.
                self._fail_action(str(exc))
                return
            if seconds is not None:
                # Assigned unconditionally: a declared timeout always replaces
                # `default_timeout`, including when it is too large to enforce
                # (in which case the action runs unbounded — see
                # _timeout_from_seconds). Assigning only in the enforceable
                # case would silently downgrade an oversized timeout to the
                # positional default, e.g. an environment exit's 5 minutes.
                time_limit = _timeout_from_seconds(seconds, self._logger)
            # Resolve the action's effective cancelation NOW, against the
            # same final scope the command/args/timeout resolved with — a
            # deferred (format-string) mode or FEATURE_BUNDLE_1 notify
            # period may reference script-level `let` bindings or
            # Env.File.*/Task.File.* symbols that only exist in this scope.
            # This matches the openjd-rs runtime (run_action resolves
            # cancel_method_for_action before launching): an unresolvable or
            # invalid cancelation fails the action at start instead of
            # surfacing only if a cancel later occurs. The runners'
            # cancel() consumes the stored method.
            try:
                mode, period = resolve_effective_cancelation(action.cancelation, symtab)
            except ValueError as exc:
                # FormatStringError (resolution failure) subclasses ValueError.
                self._fail_action(str(exc))
                return
            if mode != CancelationMode_2023_09.NOTIFY_THEN_TERMINATE.value:
                # Note: The default cancelation for a 2023-09 script is Terminate
                self._resolved_cancel_method = TerminateCancelMethod()
            else:
                self._resolved_cancel_method = NotifyCancelMethod(
                    terminate_delay=timedelta(
                        seconds=(period if period is not None else default_notify_period_seconds)
                    )
                )
            self._run(command, time_limit)

    def _cancel_with_resolved_method(
        self, time_limit: Optional[timedelta] = None, mark_action_failed: bool = False
    ) -> None:
        """Shared implementation of the runners' :meth:`cancel`: cancel with
        the effective cancel method that :meth:`_run_action` resolved at
        launch time.

        A cancel that arrives before the subprocess exists is remembered and
        applied by :meth:`_run` as soon as it does (see :attr:`_pending_cancel`)
        — `cancel()` is called from another thread, so "no subprocess yet" is a
        race, not a no-op. Only when no action will ever be launched (setup
        failed before resolution) is there genuinely nothing to cancel.
        """
        if self._process is None:
            if self.state == ScriptRunnerState.FAILED:
                # Setup already failed; no subprocess is coming.
                return
            self._pending_cancel = (time_limit, mark_action_failed)
            return
        # Note: If the given time_limit is less than that in the method, then the time_limit will be what's used.
        if self._resolved_cancel_method is None:  # pragma: no cover - defensive
            return
        self._cancel(self._resolved_cancel_method, time_limit, mark_action_failed)

    def _cancel(
        self,
        method: CancelMethod,
        time_limit: Optional[timedelta] = None,
        mark_action_failed: bool = False,
    ) -> None:
        if self._process is None:
            # A cancel that raced action setup: nothing to signal yet. Callers
            # go through _cancel_with_resolved_method, which records it as a
            # pending cancel instead — an early return here rather than an
            # assert, so no bare AssertionError can reach the public API.
            return
        # Nothing to do if it's not running.
        if not self._process.is_running:
            return

        with self._lock:
            self._canceled = True
            self._notify_canceled_action_as_failed = mark_action_failed
            now = datetime.now(timezone.utc)
            now_str = now.strftime(TIME_FORMAT_STR)
            if self._cancel_gracetime_timer is not None:
                # This cancel request is a duplicate that may have a different gracetime.
                # We'll recalculate the gracetime
                self._cancel_gracetime_timer.cancel()
                self._cancel_gracetime_timer = None

            if isinstance(method, TerminateCancelMethod):
                self._logger.info(
                    f"Canceling subprocess {str(self._process.pid)} via termination method at {now_str}.",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )
                try:
                    self._process.terminate()
                except OSError as err:  # pragma: nocover
                    # Being paranoid. Won't happen... if we could start the process, then we can send it a signal
                    self._logger.warning(
                        f"Cancelation could not send terminate signal to process {self._process.pid}: {str(err)}",
                        extra=LogExtraInfo(
                            openjd_log_content=LogContent.PROCESS_CONTROL
                            | LogContent.EXCEPTION_INFO
                        ),
                    )
            else:
                self._logger.info(
                    f"Canceling subprocess {str(self._process.pid)} via notify then terminate method at {now_str}.",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )
                method = cast(NotifyCancelMethod, method)

                gracetime = (
                    min(time_limit, method.terminate_delay)
                    if time_limit is not None
                    else method.terminate_delay
                )
                if self._cancel_gracetime_end is not None:
                    # How much time is remaining in the previous cancel?
                    time_remaining = self._cancel_gracetime_end - now
                    # Our gracetime is the minimum of remaining and the new time limit
                    gracetime = min(gracetime, time_remaining)
                self._cancel_gracetime_end = now + gracetime

                # 1) Create the notification file
                #      Note: Notify-then-terminate requires writing a "cancel_info.json" file to
                #      the session working directory. Contents are JSON formatted with contents:
                #      { "NotifyEnd": "<yyyy>-<mm>-<dd>T<hh>:<mm>:<ss>Z" }
                #      where the given time is the time at which the notify period will end (i.e. when
                #      when we'll send the SIGKILL)
                grace_end_time_str = self._cancel_gracetime_end.strftime(TIME_FORMAT_STR)
                notify_end = json.dumps({"NotifyEnd": grace_end_time_str})
                write_file_for_user(
                    self._session_working_directory / "cancel_info.json", notify_end, self._user
                )
                self._logger.info(
                    f"Grace period ends at {grace_end_time_str}",
                    extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
                )

                # 2) Send the notify
                try:
                    self._process.notify()
                except OSError as err:  # pragma: nocover
                    # Being paranoid. Won't happen... if we could start the process, then we can send it a signal
                    self._logger.warning(
                        f"Cancelation could not send notify signal to process {self._process.pid}: {str(err)}",
                        extra=LogExtraInfo(
                            openjd_log_content=LogContent.PROCESS_CONTROL
                            | LogContent.EXCEPTION_INFO
                        ),
                    )

                # 4) Set up the timer to send the terminate signal
                self._cancel_gracetime_timer = Timer(
                    gracetime.total_seconds(), self._on_notify_period_end
                )
                self._cancel_gracetime_timer.start()

    def _on_process_exit(self, future: Future) -> None:
        """This is invoked as a callback when run_future is done."""
        assert self._run_future is not None
        with self._lock:
            if self._runtime_limit is not None:
                self._runtime_limit.cancel()
                self._runtime_limit = None

            if self._cancel_gracetime_timer is not None:
                self._cancel_gracetime_timer.cancel()
                self._cancel_gracetime_timer = None

            if exc := self._run_future.exception():
                self._logger.error(
                    f"Error running subprocess: {str(exc)}",
                    extra=LogExtraInfo(
                        openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                    ),
                )

            if self._callback is not None:
                self._callback(ActionState(self.state.value))

    def _on_notify_period_end(self) -> None:
        """This is invoked when the grace period in a NOTIFY_THEN_TERMINATE
        cancelation has expired.
        """
        assert self._process is not None
        with self._lock:
            self._cancel_gracetime_timer = None
        self._logger.info(
            "Notify period ended. Terminate at %s",
            datetime.now(timezone.utc).strftime(TIME_FORMAT_STR),
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        try:
            self._process.terminate()
        except OSError as err:  # pragma: nocover
            # Being paranoid. Won't happen... if we could start the process, then we can send it a kill signal
            self._logger.warning(
                f"Cancelation could not send terminate signal to process {self._process.pid}: {str(err)}",
                extra=LogExtraInfo(
                    openjd_log_content=LogContent.PROCESS_CONTROL | LogContent.EXCEPTION_INFO
                ),
            )

    def _on_timelimit(self) -> None:
        """Callback that is invoked when the runtime limit of the running
        process has expired.
        """
        with self._lock:
            self._runtime_limit = None
        self._logger.info(
            "TIMEOUT - Runtime limit reached at %s. Canceling action.",
            datetime.now(timezone.utc).strftime(TIME_FORMAT_STR),
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )
        self._runtime_limit_reached = True
        self.cancel()
