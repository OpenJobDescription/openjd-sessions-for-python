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
