# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Open Job Description Session — thin wrapper over Rust implementation."""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from openjd._openjd_rs import (
    Session as _RustSession,
    SessionState,
    ActionState,
    ActionStatus,
    PathMappingRule,
)
from openjd.expr import SerializedSymbolTable
from openjd.model._v1.types import JobParameterValue, ModelProfile

from ._session_user import SessionUser
from ._types import (
    EnvironmentIdentifier,
    EnvironmentModel,
    StepScriptModel,
)

# Bridge Rust logging (openjd_sessions) to Python logging (openjd.sessions).
# pyo3-log sends Rust log records to Python logger "openjd_sessions" (underscore),
# but the CLI/worker attach handlers to "openjd.sessions" (dot). We redirect by
# adding the Python logger's handlers to the Rust logger.
import logging as _logging

_rust_logger = _logging.getLogger("openjd_sessions")
_py_logger = _logging.getLogger("openjd.sessions")
_rust_logger.parent = _py_logger
_rust_logger.setLevel(_logging.DEBUG)

SessionCallbackType = Callable[[str, ActionStatus], None]

__all__ = [
    "ActionStatus",
    "Session",
    "SessionCallbackType",
    "SessionState",
]

JobParameterValues = dict[str, JobParameterValue]
TaskParameterSet = dict[str, Any]


class Session:
    """A context for running actions of an Open Job Description Job."""

    _rust_session: _RustSession
    _callback: Optional[SessionCallbackType]
    _session_id: str
    # Tracks whether the current action's RUNNING transition has already been
    # delivered to self._callback. Set True by `_fire_initial_running_callback`
    # which runs synchronously inside the action-start methods; consumed by
    # the polling thread's `reported_running` so we don't double-fire.
    _running_reported: bool

    def __init__(
        self,
        *,
        session_id: str,
        job_parameter_values: JobParameterValues,
        path_mapping_rules: Optional[list[PathMappingRule]] = None,
        retain_working_dir: bool = False,
        user: Optional[SessionUser] = None,
        callback: Optional[SessionCallbackType] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        session_root_directory: Optional[Path] = None,
        profile: Optional[ModelProfile] = None,
    ):
        self._session_id = session_id
        self._callback = callback
        self._running_reported = False

        self._rust_session = _RustSession(
            session_id=session_id,
            job_parameter_values=job_parameter_values or {},
            path_mapping_rules=path_mapping_rules,
            retain_working_dir=retain_working_dir,
            os_env_vars=os_env_vars,
            session_root_directory=str(session_root_directory) if session_root_directory else None,
            user=user,
            profile=profile,
        )

    def _fire_initial_running_callback(self) -> None:
        """Synchronously deliver the action's RUNNING-transition callback.

        Called from `enter_environment` / `exit_environment` / `run_task` /
        `run_subprocess` _before_ spawning the polling thread. This matches
        the mainline (non-Rust) `_action_callback` semantics: by the time
        the action-start method returns to the worker agent, the agent's
        callback has already been fired with state=RUNNING and a
        `started_at` time set to "now".

        Why we don't just rely on the polling thread:
        the worker agent's scheduler (`Session._start_action`) does not
        record an entry in `_action_updates_map` — and therefore cannot
        send `startedAt` to the Deadline service — until the openjd-sessions
        callback fires for the first time. The polling thread's first
        callback is delivered asynchronously, up to ~50 ms (often more
        under load) after the Rust session transitions to Running. In that
        window the agent's next `_sync()` ticks fire UpdateWorkerSchedule
        with `updatedSessionActions: {}`. The service interprets that as
        "agent has capacity" and may assign another session and/or
        auto-finalize the un-acknowledged one as FAILED, after which the
        agent's belated `{startedAt, updatedAt}` is rejected with
        `UnknownSessionActionStatus(0)` and the agent crashes.

        Synthesizing a RUNNING ActionStatus here (with progress/messages
        from whatever the Rust session has surfaced so far, falling back
        to defaults) bridges that gap without needing to plumb a new
        callback path through the PyO3 binding.

        We set `_running_reported = True` so the polling thread doesn't
        double-fire; its phase 1 turns into a "watch for directive
        changes" loop instead of a "wait for first RUNNING" loop.
        """
        if self._callback is None:
            self._running_reported = True
            return

        status = self._rust_session.action_status
        # Synthesize a RUNNING ActionStatus. The Rust session may not have
        # populated action_status yet (it deliberately clears it on the
        # transition to Running and only sets it once the Rust callback
        # fires, which is the same race we're fixing). Default the directive
        # fields to None / 0.0 in that case.
        running_status = ActionStatus(
            state=ActionState.RUNNING,
            exit_code=None,
            fail_message=None,
            progress=status.progress if status is not None else 0.0,
            status_message=status.status_message if status is not None else None,
        )
        self._callback(self._session_id, running_status)
        self._running_reported = True

    def _poll_for_completion(self):
        """Watch the Rust session and fire callbacks as the action transitions.

        Callback contract (paired with `_fire_initial_running_callback`):
          - The initial RUNNING callback is delivered synchronously by the
            caller (via `_fire_initial_running_callback`) before this
            polling thread is spawned. By the time we start polling, the
            agent has already recorded `started_at`.
          - Additional callbacks while RUNNING whenever any of the three
            ActionStatus fields driven by `openjd_*` directives changes:
            progress (`openjd_progress`), status_message (`openjd_status`),
            or fail_message (`openjd_fail`). These keep the worker agent's
            progressPercent/progressMessage live mid-action.
          - One callback when the action ends (state != RUNNING).

        We guard against:
          1. Racing between action start and this poll loop — the Rust session
             may already be back to READY by the time we enter the loop if the
             subprocess ran in < 10ms. The synchronous RUNNING callback was
             already delivered; we only need to deliver the terminal status.
          2. Stale callbacks from previous actions — each call to a run_*
             method starts a fresh poll thread; we only observe the session's
             current action_status.
          3. Duplicate callbacks for the same observable state — the worker
             agent's `_action_updated_impl` is keyed off action-id, not state,
             but firing redundant identical updates wastes UpdateWorkerSchedule
             API calls.
        """
        # Snapshot _running_reported so the closure sees the synchronous
        # callback's effect; reset the instance flag for the next action.
        reported_running_initially = self._running_reported
        self._running_reported = False

        def _poll():
            reported_running = reported_running_initially
            last_observable: Optional[tuple] = None  # (progress, status_message, fail_message)

            def observable(s: ActionStatus) -> tuple:
                return (s.progress, s.status_message, s.fail_message)

            # Watch for changes in the openjd_* directive fields and the
            # eventual transition out of RUNNING.
            while True:
                state = self._rust_session.state
                status = self._rust_session.action_status
                if state == SessionState.RUNNING:
                    if status is not None and self._callback:
                        if not reported_running:
                            # Defensive: the synchronous initial callback
                            # should have set reported_running, but keep the
                            # fallback in case _fire_initial_running_callback
                            # was bypassed (e.g. by a future code path).
                            reported_running = True
                            last_observable = observable(status)
                            self._callback(self._session_id, status)
                        elif observable(status) != last_observable:
                            last_observable = observable(status)
                            self._callback(self._session_id, status)
                    time.sleep(0.05)
                    continue
                # Not RUNNING anymore — action is done.
                # If we never saw RUNNING (e.g. Rust finished before we got here),
                # still report the RUNNING transition first, so the agent state
                # machine sees Start → End in order.
                if not reported_running and status is not None and self._callback:
                    running_status = ActionStatus(
                        state=ActionState.RUNNING,
                        exit_code=None,
                        fail_message=None,
                        progress=status.progress,
                        status_message=status.status_message,
                    )
                    self._callback(self._session_id, running_status)
                    reported_running = True
                # Report terminal state.
                if status is not None and self._callback:
                    self._callback(self._session_id, status)
                return

        t = threading.Thread(target=_poll, daemon=True)
        t.start()

    @property
    def working_directory(self) -> Path:
        return Path(self._rust_session.working_directory)

    @property
    def files_directory(self) -> Path:
        return Path(self._rust_session.files_directory)

    @property
    def state(self) -> SessionState:
        return self._rust_session.state

    @property
    def action_status(self) -> Optional[ActionStatus]:
        return self._rust_session.action_status

    @property
    def environments_entered(self) -> tuple[EnvironmentIdentifier, ...]:
        return tuple(self._rust_session.environments_entered)

    def cancel_action(self, *, time_limit=None, mark_action_failed=False) -> None:
        seconds = time_limit.total_seconds() if time_limit else None
        self._rust_session.cancel_action(seconds, mark_action_failed)

    def enter_environment(
        self,
        *,
        environment: EnvironmentModel,
        identifier: Optional[EnvironmentIdentifier] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        resolved_symtab: Optional[SerializedSymbolTable] = None,
    ) -> EnvironmentIdentifier:
        # ``resolved_symtab`` is the step-scope symbol table generated
        # by ``create_job`` (available as ``Step.resolved_symtab``).
        # It contains ``Param.*``, ``RawParam.*``, ``Job.Name``,
        # ``Step.Name``, and the step-level let-binding values. Without
        # it, the runner sees an empty symtab and any ``{{ Param.X }}``
        # interpolation or expression in the environment script will
        # fail with ``Undefined variable``. ``None`` is fine when the
        # environment script doesn't reference any of those names.
        eid = self._rust_session.enter_environment(
            environment=environment,
            identifier=identifier,
            resolved_symtab=resolved_symtab,
            os_env_vars=os_env_vars,
        )
        self._fire_initial_running_callback()
        self._poll_for_completion()
        return eid

    def exit_environment(
        self,
        *,
        identifier: EnvironmentIdentifier,
        os_env_vars: Optional[dict[str, str]] = None,
        keep_session_running: bool = True,
        resolved_symtab: Optional[SerializedSymbolTable] = None,
    ) -> None:
        # See ``enter_environment`` for ``resolved_symtab`` semantics.
        self._rust_session.exit_environment(
            identifier=identifier,
            resolved_symtab=resolved_symtab,
            keep_session_running=keep_session_running,
            os_env_vars=os_env_vars,
        )
        self._fire_initial_running_callback()
        self._poll_for_completion()

    def run_task(
        self,
        *,
        step_script: StepScriptModel,
        task_parameter_values: TaskParameterSet,
        os_env_vars: Optional[dict[str, str]] = None,
        log_task_banner: bool = True,
        resolved_symtab: Optional[SerializedSymbolTable] = None,
    ) -> None:
        # ``resolved_symtab`` is the step-scope symbol table generated
        # by ``create_job`` (available as ``Step.resolved_symtab``).
        # It contains ``Param.*``, ``RawParam.*``, ``Job.Name``,
        # ``Step.Name``, and the step-level let-binding values. The
        # runner layers ``Session.*`` and ``Task.*`` values on top to
        # evaluate the script-level let bindings and the action
        # arguments. ``None`` is fine when the script has no let
        # bindings and no expression interpolation that depends on
        # step-scope state.
        #
        # ``log_task_banner`` is accepted for API compatibility but
        # currently has no effect — the Rust runner always emits the
        # task banner. TODO: plumb through if/when the Rust API
        # supports suppressing it.
        self._rust_session.run_task(
            step_script=step_script,
            task_parameter_values=task_parameter_values,
            resolved_symtab=resolved_symtab,
            os_env_vars=os_env_vars,
        )
        self._fire_initial_running_callback()
        self._poll_for_completion()

    def run_subprocess(
        self,
        *,
        command: str,
        args: Optional[list[str]] = None,
        timeout: Optional[int] = None,
        os_env_vars: Optional[dict[str, str]] = None,
        use_session_env_vars: bool = True,
        log_banner_message: Optional[str] = None,
    ) -> None:
        self._rust_session.run_subprocess(
            command=command,
            args=args,
            timeout=float(timeout) if timeout else None,
            os_env_vars=os_env_vars,
            use_session_env_vars=use_session_env_vars,
            log_banner_message=log_banner_message,
        )
        self._fire_initial_running_callback()
        self._poll_for_completion()

    def cleanup(self) -> None:
        self._rust_session.cleanup()

    def __enter__(self) -> "Session":
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_value: Optional[BaseException],
        traceback: Optional[Any],
    ) -> None:
        self.cleanup()

    def extend_path_mapping_rules(self, additional: list[PathMappingRule]) -> None:
        """Append additional path mapping rules to this session's rule set.

        Forwards to the Rust Session.extend_path_mapping_rules, which re-sorts
        rules by source-path length (longest first) so the most specific rule
        matches first during FormatString resolution.

        Consumers like the Deadline Cloud worker agent call this between
        actions — after an assigned action delivers per-storage-profile or
        per-attachment path mappings — to extend the rules set up at session
        construction time.

        Raises
        ------
        RuntimeError
            If an action is currently in-flight. Call between actions only.
        """
        self._rust_session.extend_path_mapping_rules(additional)

    def get_enabled_extensions(self) -> list[str]:
        return []

    def __del__(self):
        self.cleanup()
