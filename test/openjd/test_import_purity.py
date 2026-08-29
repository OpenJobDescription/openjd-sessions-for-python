# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""``openjd.sessions`` must not load the native EXPR extension unless used.

``openjd.expr`` is a thin facade over the native ``openjd._openjd_rs``
extension. A module-level import of it anywhere on the pure-Python session
path makes the extension a *load-time* requirement of ``openjd.sessions``, so
a consumer (e.g. the Deadline Cloud worker agent) that only runs non-EXPR
templates can no longer start without a working native extension for its
platform. openjd.model keeps the Rust surface off its import path deliberately
(see ``openjd.model._format_strings._parser``, which duplicates a constant
rather than import it); these tests hold openjd.sessions to the same contract,
at import time *and* while resolving a non-EXPR template.

Each check runs in a fresh interpreter, because by the time any given test runs
the extension may already have been loaded by another test in the same worker.

Note for coverage readers: these probes run in child processes with no
``COVERAGE_PROCESS_START``, so the guard in ``_runner_base._is_expr_null``
that they exercise still reports as uncovered. Do not "fix" that by deleting
the guard -- it is what these tests exist to protect.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Kept below the suite-wide ``--timeout=30`` (see pyproject addopts) so that a
# hung probe is reported as a probe timeout with its output, rather than
# pytest-timeout killing the test first and losing it.
_PROBE_TIMEOUT_SECONDS = 20

# Injected ahead of every probe body, so a body never needs interpolation --
# probe bodies contain OpenJD ``{{ ... }}`` format strings, which an f-string
# or ``str.format`` would silently mangle into ``{ ... }``.
_PROBE_PREAMBLE = """\
import json
import sys

sys.path[:] = json.loads(sys.argv[1])

RS = "openjd._openjd_rs"
"""


def _probe_python() -> str:
    """The interpreter to run a probe with.

    Mirrors the ``python_exe`` fixture in ``sessions_v0/conftest.py``: under
    Windows Session 0 ``sys.executable`` is ``pythonservice.exe``, which cannot
    run a script.
    """
    if sys.platform == "win32" and "pythonservice.exe" in sys.executable.lower():
        return sys.executable.lower().replace("pythonservice.exe", "python.exe")
    return sys.executable


def _run_probe(tmp_path: Path, body: str) -> str:
    """Run ``body`` in a fresh interpreter and return its stdout, stripped.

    ``body`` may use the name ``RS`` (the native extension's module name) and
    ``sys``. It is written to a script file rather than passed with ``-c`` so
    that a traceback is inspectable, and ``sys.path`` is handed over as a JSON
    argument -- not embedded in the source -- so that a path which is not
    encodable as UTF-8 source cannot break the probe.
    """
    script = tmp_path / "probe.py"
    script.write_text(_PROBE_PREAMBLE + textwrap.dedent(body), encoding="utf-8")
    completed = subprocess.run(
        [_probe_python(), str(script), json.dumps(sys.path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, (
        f"probe exited {completed.returncode}\n"
        f"--- script ---\n{script.read_text(encoding='utf-8')}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    return completed.stdout.strip()


def test_importing_sessions_does_not_load_native_extension(tmp_path: Path) -> None:
    """The 0.10.11 regression: a module-level ``from openjd.expr import ...``
    on the pure-Python path makes ``import openjd.sessions`` load the native
    extension."""
    # WHEN
    loaded = _run_probe(
        tmp_path,
        """
        import openjd.sessions

        print(RS in sys.modules)
        """,
    )

    # THEN
    assert loaded == "False", (
        "`import openjd.sessions` loaded the native extension. Something on the "
        "pure-Python session path imports openjd.expr (or openjd._openjd_rs) at "
        "module level; it must be imported lazily instead."
    )


def test_importing_sessions_does_not_resolve_openjd_expr(tmp_path: Path) -> None:
    """``openjd.expr`` is the facade the extension gets pulled in through, so
    it must not appear either -- this still fails on a module-level import if
    some future build made the extension itself lazy."""
    # WHEN
    resolved = _run_probe(
        tmp_path,
        """
        import openjd.sessions

        print("openjd.expr" in sys.modules)
        """,
    )

    # THEN
    assert resolved == "False", (
        "`import openjd.sessions` resolved openjd.expr at import time; it must "
        "only be imported from inside a function on the EXPR path."
    )


def test_native_extension_is_available(tmp_path: Path) -> None:
    """Negative control: the extension really is installed here. Without this,
    every "must not be loaded" test above would pass trivially in an
    environment where it simply is not present."""
    # WHEN
    loaded = _run_probe(
        tmp_path,
        """
        import openjd.expr

        print(RS in sys.modules)
        """,
    )

    # THEN
    assert loaded == "True", (
        "openjd.expr did not load the native extension, so the purity tests in "
        "this file prove nothing. Check the openjd-model install."
    )


def test_probe_detects_a_loaded_extension(tmp_path: Path) -> None:
    """Negative control for the probe harness: an extension loaded *before*
    ``openjd.sessions`` must be observed as loaded. Guards against the checks
    above passing because ``sys.modules`` is being read wrongly."""
    # WHEN
    loaded = _run_probe(
        tmp_path,
        """
        import openjd.expr
        import openjd.sessions

        print(RS in sys.modules)
        """,
    )

    # THEN
    assert loaded == "True", "the probe cannot observe a loaded extension at all"


def test_resolving_a_non_expr_field_does_not_load_native_extension(
    tmp_path: Path,
) -> None:
    """Import-time purity is not enough: *running* a non-EXPR template must
    not load the extension either.

    ``_is_expr_null`` runs for every optional int field (an action's
    ``timeout``, a cancelation's ``notifyPeriodInSeconds``). Its
    ``sys.modules`` guard is what keeps that path extension-free. A type-based
    guard is not sufficient and this test is why: a legacy whole-field
    interpolation returns the symbol's own value, so ``resolve_value`` here
    returns an ``int`` -- not a ``str`` and not an ``ExprValue``.
    """
    # WHEN: a legacy (non-EXPR) interpolation resolves against an int symbol.
    loaded = _run_probe(
        tmp_path,
        """
        from openjd.model import SymbolTable
        from openjd.model.v2023_09 import Action, ModelParsingContext
        from openjd.sessions._runner_base import resolve_optional_int_field

        context = ModelParsingContext(supported_extensions=["FEATURE_BUNDLE_1"])
        action = Action.model_validate(
            {"command": "echo", "timeout": "{{ Param.Seconds }}"}, context=context
        )
        symtab = SymbolTable()
        symtab["Param.Seconds"] = 30

        resolved = resolve_optional_int_field(
            action.timeout, symtab, ge=1, description="timeout"
        )
        assert resolved == 30, resolved
        print(RS in sys.modules)
        """,
    )

    # THEN
    assert loaded == "False", (
        "resolving a non-EXPR timeout loaded the native extension. The "
        "`sys.modules` guard in _runner_base._is_expr_null is the thing that "
        "prevents this; check it has not been replaced by a type-based test."
    )


def test_resolving_an_expr_field_still_resolves_null(tmp_path: Path) -> None:
    """Negative control for over-correction: the EXPR path must still work.

    Deliberately asserts the resolved *value*, not ``sys.modules``: parsing an
    EXPR-extension template already loads the extension inside openjd.model,
    so a ``sys.modules`` assertion here would hold no matter what
    ``_is_expr_null`` did. A whole-field EXPR expression resolving to null must
    yield ``None`` ("field omitted"), which is exactly the branch the lazy
    ``ExprValue`` import serves -- if it stopped working, the strict integer
    parse would raise instead.
    """
    # WHEN
    resolved = _run_probe(
        tmp_path,
        """
        from openjd.model import SymbolTable
        from openjd.model.v2023_09 import Action, ModelParsingContext
        from openjd.sessions._runner_base import resolve_optional_int_field

        context = ModelParsingContext(supported_extensions=["FEATURE_BUNDLE_1", "EXPR"])
        action = Action.model_validate(
            {"command": "echo", "timeout": "{{ Seconds }}"}, context=context
        )
        symtab = SymbolTable()
        symtab["Seconds"] = None

        print(repr(resolve_optional_int_field(
            action.timeout, symtab, ge=1, description="timeout"
        )))
        """,
    )

    # THEN
    assert resolved == "None", (
        "a whole-field EXPR expression resolving to null must be treated as "
        f"'field omitted' (None), got {resolved}"
    )


@pytest.mark.parametrize(
    "module",
    [
        "openjd.sessions._runner_base",
        "openjd.sessions._session",
        "openjd.sessions._runner_step_script",
        "openjd.sessions._runner_env_script",
    ],
)
def test_pure_path_module_does_not_load_native_extension(tmp_path: Path, module: str) -> None:
    """Importing any individual pure-Python-path module must stay extension
    free, so the contract does not silently depend on ``__init__`` ordering.

    ``openjd.sessions._v1`` is deliberately excluded: it is the opt-in
    Rust-backed API surface, is not reachable from ``openjd.sessions``, and is
    expected to load the extension.
    """
    # WHEN
    loaded = _run_probe(
        tmp_path,
        f"""
        import {module}

        print(RS in sys.modules)
        """,
    )

    # THEN
    assert loaded == "False", f"importing {module} loaded the native extension"


# ---------------------------------------------------------------------------
# Import-time purity is not enough on its own: `Session.__init__` calls
# `_build_expr_host_rules()` unconditionally, so before the empty-rules fast
# path a worker that never evaluates an EXPR expression still loaded the
# extension on its FIRST SESSION. The deferral won by removing the module-level
# import was only from import time to first-session time.
# ---------------------------------------------------------------------------


class TestSessionLifecycleStaysExtensionFree:
    def test_constructing_a_session_without_path_mapping_rules_stays_pure(
        self, tmp_path: Path
    ) -> None:
        """The regression: `Session()` used to load the extension to build an
        empty engine rules list."""
        # WHEN
        loaded = _run_probe(
            tmp_path,
            """
            import uuid

            from openjd.sessions import Session

            session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
            try:
                # `[]`, not `None`: the session is still host scope, so
                # apply_path_mapping() stays available with an empty rule set.
                assert session._expr_host_rules == [], session._expr_host_rules
                print(RS in sys.modules)
            finally:
                session.cleanup()
            """,
        )

        # THEN
        assert loaded == "False", (
            "constructing a Session with no path mapping rules loaded the native "
            "extension; the empty-rules fast path in _build_expr_host_rules is "
            "what prevents this."
        )

    def test_running_a_non_expr_task_stays_pure_end_to_end(self, tmp_path: Path) -> None:
        """The property a non-EXPR worker actually cares about: a whole task
        runs without the extension ever being loaded."""
        # WHEN
        loaded = _run_probe(
            tmp_path,
            """
            import time
            import uuid

            from openjd.model.v2023_09 import ModelParsingContext, StepScript
            from openjd.sessions import ActionState, Session, SessionState

            context = ModelParsingContext(supported_extensions=["FEATURE_BUNDLE_1"])
            script = StepScript.model_validate(
                {"actions": {"onRun": {"command": "echo", "args": ["hello"]}}},
                context=context,
            )
            session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
            try:
                session.run_task(step_script=script, task_parameter_values={})
                deadline = time.time() + 15
                while session.state == SessionState.RUNNING and time.time() < deadline:
                    time.sleep(0.05)
                status = session.action_status
                assert status is not None and status.state == ActionState.SUCCESS, status
                print(RS in sys.modules)
            finally:
                session.cleanup()
            """,
        )

        # THEN
        assert loaded == "False", (
            "running a non-EXPR task loaded the native extension somewhere in the "
            "session lifecycle"
        )

    def test_path_mapping_rules_do_load_the_extension(self, tmp_path: Path) -> None:
        """Negative control, and a documented limitation rather than a goal.

        Real rules must be converted into ``openjd.expr.PathMappingRule`` engine
        objects to seed the session's host context, so this case genuinely loads
        the extension. openjd-sessions cannot avoid it alone: closing it needs
        openjd-model to accept unconverted rules and convert them at the Rust
        boundary, where ``symtab_to_expr_values`` already crosses.

        Asserted so that the limitation is visible and so a future change that
        makes this pure is noticed here rather than passing silently.
        """
        # WHEN
        loaded = _run_probe(
            tmp_path,
            """
            import uuid
            from pathlib import PurePosixPath

            from openjd.sessions import PathFormat, PathMappingRule, Session

            rule = PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/mnt/source"),
                destination_path=PurePosixPath("/mnt/dest"),
            )
            session = Session(
                session_id=uuid.uuid4().hex,
                job_parameter_values={},
                path_mapping_rules=[rule],
            )
            try:
                assert len(session._expr_host_rules) == 1, session._expr_host_rules
                print(RS in sys.modules)
            finally:
                session.cleanup()
            """,
        )

        # THEN
        assert loaded == "True", (
            "a session with real path mapping rules is expected to load the "
            "extension; if this is now False the limitation has been fixed and "
            "this control should become a purity assertion"
        )


# ---------------------------------------------------------------------------
# `apply_script_let_bindings` imports `openjd.expr.PathFormat` to pin a step
# script's template-scope `let` prefix to POSIX. A function-local import is not
# enough on its own (rule: lazy is not conditional) -- the enclosing function is
# reachable from every script that has any `let` at all, so the import has to sit
# behind "there is a template-scope prefix to evaluate".
#
# Both probes use a MALFORMED binding, which openjd-model skips without parsing.
# That removes the evaluation itself as a possible cause of the load, leaving the
# PathFormat import as the only crossing either probe can observe.
# ---------------------------------------------------------------------------


_LET_SPLIT_PROBE = """
from openjd.model import SymbolTable
from openjd.sessions._runner_base import apply_script_let_bindings


class Script:
    _template_scope_let_count = %d


apply_script_let_bindings(
    symtab=SymbolTable(), let_bindings=["malformed"], script=Script()
)
print(RS in sys.modules)
"""


def test_a_let_list_with_no_template_scope_prefix_stays_pure(tmp_path: Path) -> None:
    """A script whose ``let`` is entirely its own -- the only shape a non-EXPR
    template can even produce -- must not reach the ``PathFormat`` import."""
    # WHEN
    loaded = _run_probe(tmp_path, _LET_SPLIT_PROBE % 0)

    # THEN
    assert loaded == "False", (
        "evaluating a let list with no template-scope prefix loaded the native "
        "extension. The `if template_scope_count:` guard around the PathFormat "
        "import in apply_script_let_bindings is what prevents this; a bare "
        "function-local import is not sufficient."
    )


def test_a_template_scope_prefix_does_load_the_extension(tmp_path: Path) -> None:
    """Positive control for the probe above. Without it, ``False`` would be
    indistinguishable from the probe being unable to observe the load at all --
    and it confirms the guarded import is the only crossing on this path, since
    the malformed binding is never parsed."""
    # WHEN
    loaded = _run_probe(tmp_path, _LET_SPLIT_PROBE % 1)

    # THEN
    assert loaded == "True", (
        "a template-scope prefix must load the extension to reach "
        "PathFormat.POSIX; if this is False the prefix is no longer being pinned "
        "to POSIX at all"
    )
