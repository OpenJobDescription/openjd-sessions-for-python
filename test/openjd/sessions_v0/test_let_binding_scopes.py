# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A session evaluates exactly one scope of ``let`` bindings: the script's own.

A step's *template*-scope ``let`` is resolved once at job creation, with
``PathFormat::Posix`` so a create-time value cannot depend on the host that
created the job. Those resolved values reach a session in the step symbol table
(``Step.resolved_symtab``) and are seeded by
:meth:`Session._resolved_base_entries`, deserialized into the host's format. A
script's own ``let`` is session scope and is evaluated here, in the host's
format, against the live session symbols.

The two must not be confused, and the failure mode is asymmetric. Both a seeded
value and a session-time re-evaluation land in the *same* symbol table, so when
both happen the re-evaluation writes **last** and clobbers the correctly
formatted seeded value. That overwrite is the bug these tests exist to prevent.

What :class:`TestSeededStepValuesAreNotReEvaluated` pins, measured rather than
assumed, is the *host-format deserialization* of ``resolved_symtab``: forcing
:mod:`openjd.sessions._session`'s ``host_format`` to POSIX fails it. It does not
by itself fail if the model starts re-merging a step's bindings into the script,
because it builds the script's ``let`` list itself rather than getting one from
job creation. That other half is pinned model-side, by
``TestStepLetIsNotMergedIntoScript`` in
``test/openjd/model_v0/v2023_09/test_let_bindings.py``, whose six cases all fail
against the pre-fix ``_model.py``. Together the two cover the clobber; neither
covers it alone.

On simulating a Windows host. A POSIX host renders both scopes identically, so a
value comparison here proves nothing about format on this machine -- it would
pass whatever the code did. ``_windows_host`` forces the other format, and it
patches **both** seams that choose one:

- ``openjd.sessions._session.os.name``, which
  :meth:`Session._resolved_base_entries` reads to pick the format it
  deserializes a create-time table with; and
- ``ExprNode._evaluate_raw``'s ``path_format=None`` default, which is the engine
  default and is POSIX on this host.

Patching only the first is not a Windows host, it is a self-inconsistent one:
seeded values would render Windows while a script's own ``let`` still rendered
POSIX, and a test built on that would be asserting an arrangement that cannot
occur in production.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from pathlib import PureWindowsPath
from typing import Any, Generator, Optional
from unittest.mock import patch as mock_patch

import pytest

from openjd.expr import PathFormat, SerializedSymbolTable
from openjd.model import SpecificationRevision, SymbolTable, evaluate_let_bindings
from openjd.model._format_strings._nodes import ExprNode
from openjd.model.v2023_09 import (
    ModelParsingContext as ModelParsingContext_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import Session
from openjd.sessions._runner_base import apply_let_bindings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEEDED_NAME = "step_out"
"""The name a step-level (template-scope) ``let`` binding resolved to at job
creation, arriving in the session's ``resolved_symtab``."""

_SEEDED_POSIX_TEXT = "/foo/bar"
"""The create-time value, as the service serialized it."""

_SEEDED_WINDOWS_TEXT = r"\foo\bar"
"""The same value once deserialized in a Windows host's format, which is how a
session must read it. Distinct from ``_SEEDED_POSIX_TEXT``, which is what a
session would show if the host-format deserialization were skipped."""


@contextmanager
def _windows_host() -> Generator[None, None, None]:
    """Force a Windows path format at both seams that decide one.

    See this module's docstring for why one seam is not enough.

    Note the scope of the ``os.name`` patch: ``_session.py`` does ``import os``,
    so ``openjd.sessions._session.os`` *is* the ``os`` module and patching the
    attribute is **process-wide**, not module-scoped. It is inert today because
    ``os.name`` is read exactly once in ``_session.py``, at the seam this is
    aiming at, and nothing else runs inside the block. A module-scoped patch is
    not available without changing that import, so if you add a call inside this
    context manager, check first that it does not read ``os.name`` for an
    unrelated reason.
    """
    original = ExprNode._evaluate_raw

    def _evaluate_raw_windows(
        self: ExprNode, *, symtab: SymbolTable, path_format: Any = None
    ) -> Any:
        # Substitute only the *default*. An explicit format from a caller is
        # left alone, so this stands in for the engine default rather than
        # overriding evaluation everywhere.
        if path_format is None:
            path_format = PathFormat.WINDOWS
        return original(self, symtab=symtab, path_format=path_format)

    with mock_patch("openjd.sessions._session.os.name", "nt"):
        with mock_patch.object(ExprNode, "_evaluate_raw", _evaluate_raw_windows):
            yield


def _serialized_table(entries: list[dict[str, str]]) -> SerializedSymbolTable:
    """Build a SerializedSymbolTable from its wire (JSON) form -- the same shape
    the service serves as ``resolvedSymbolTable``."""
    return SerializedSymbolTable.from_json_str(json.dumps(entries))


def _seeded_step_table() -> SerializedSymbolTable:
    """A create-time table carrying one path-valued step-level ``let`` result."""
    return _serialized_table([{"name": _SEEDED_NAME, "type": "path", "value": _SEEDED_POSIX_TEXT}])


def _expr_step_script(let: list[str]) -> StepScript_2023_09:
    """A step script whose ``let`` is its own -- the only thing a script's ``let``
    field carries now that openjd-model no longer merges a step's bindings into
    it."""
    context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
    return StepScript_2023_09.model_validate(
        {"let": let, "actions": {"onRun": {"command": "echo", "args": ["ok"]}}},
        context=context,
    )


def _spy_on_evaluation() -> Any:
    """Patch the model's ``evaluate_let_bindings`` where openjd-sessions imports
    it, recording every call while still evaluating for real.

    Spying here rather than on ``apply_let_bindings`` keeps the real evaluation
    in the loop, so a test can assert both the calls and the resulting values.
    """
    return mock_patch(
        "openjd.sessions._runner_base.evaluate_let_bindings",
        side_effect=evaluate_let_bindings,
    )


def _evaluated_bindings(spy: Any) -> list[str]:
    """Every binding string handed to the evaluator, flattened across calls."""
    return [b for call in spy.call_args_list for b in call.kwargs["let_bindings"]]


def _session_symtab(
    session: Session,
    *,
    resolved_symtab: Optional[SerializedSymbolTable] = None,
) -> SymbolTable:
    """The session-scope symbol table a script would be resolved against.

    Built through the session's own ``_resolved_base_entries`` /
    ``_symbol_table`` rather than end to end through ``run_task``, because
    ``_windows_host`` patches the process-wide ``os.name`` and running a real
    subprocess under that would exercise Windows user and path handling on a
    POSIX host -- unrelated machinery, and not what these tests are about.
    """
    resolved_base = (
        session._resolved_base_entries(resolved_symtab) if resolved_symtab is not None else None
    )
    return session._symbol_table(
        SpecificationRevision.v2023_09,
        resolved_base=resolved_base,
    )


# ---------------------------------------------------------------------------
# The regression test for the overwrite bug.
# ---------------------------------------------------------------------------


class TestSeededStepValuesAreNotReEvaluated:
    """A create-time value seeded from ``resolved_symtab`` must survive a script
    that has its own ``let``. This is the test that fails if session-side
    re-evaluation of a step's bindings is reintroduced."""

    def test_a_seeded_path_binding_survives_a_scripts_own_let(self) -> None:
        # GIVEN: a Windows host, a create-time table carrying a path-valued
        # step-level binding, and a script with a `let` of its own.
        script = _expr_step_script(["mine = 1 + 1"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            with _windows_host():
                symtab = _session_symtab(session, resolved_symtab=_seeded_step_table())
                # The seeded value is in host format before the script's `let`
                # runs; the assertion after is that it is still there.
                assert str(symtab[_SEEDED_NAME]) == _SEEDED_WINDOWS_TEXT

                # WHEN
                with _spy_on_evaluation() as spy:
                    apply_let_bindings(symtab=symtab, let_bindings=script.let or [])

                # THEN: the seeded value is untouched, in the host's format.
                assert str(symtab[_SEEDED_NAME]) == _SEEDED_WINDOWS_TEXT, (
                    "the seeded create-time value was overwritten. A session must "
                    "read a step's resolved bindings, never re-derive them: a "
                    "re-evaluation lands in this same table and so wins."
                )
                # AND: the script's own binding did land.
                assert symtab["mine"].item() == 2
                # AND: nothing re-evaluated the seeded name. This is the half of
                # the assertion that a value comparison cannot make -- on a
                # faithful Windows host a re-evaluation of the same expression
                # would render the same text, so only the absence of the call
                # distinguishes "seeded" from "recomputed".
                assert _evaluated_bindings(spy) == ["mine = 1 + 1"]

    def test_a_step_level_binding_is_not_evaluated_at_session_time(self) -> None:
        # GIVEN: a create-time table whose step-level binding is *also* named in
        # nothing the script declares -- the shape openjd-model now produces,
        # where `script.let` holds only the script's own bindings.
        script = _expr_step_script(["mine = 'x'"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            symtab = _session_symtab(session, resolved_symtab=_seeded_step_table())

            # WHEN
            with _spy_on_evaluation() as spy:
                apply_let_bindings(symtab=symtab, let_bindings=script.let or [])

            # THEN: the evaluator saw the script's own bindings and nothing else.
            evaluated = _evaluated_bindings(spy)
            assert evaluated == ["mine = 'x'"]
            assert not any(b.split("=")[0].strip() == _SEEDED_NAME for b in evaluated), (
                f"a step-level binding ({_SEEDED_NAME}) was evaluated at session "
                "time; it is resolved at job creation and only read here"
            )


# ---------------------------------------------------------------------------
# A script's own `let` is session scope: host format, live session symbols.
# ---------------------------------------------------------------------------


class TestAScriptsOwnLetIsSessionScope:
    def test_it_evaluates_in_the_host_format_and_sees_session_symbols(self) -> None:
        # GIVEN: a Windows host and a script whose own `let` both builds a path
        # (so the format is observable) and reads a session symbol (so the
        # session scope is observable).
        script = _expr_step_script(
            [
                "built = path('/a/b')",
                "wd = Session.WorkingDirectory",
            ]
        )
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            with _windows_host():
                symtab = _session_symtab(session)

                # WHEN
                apply_let_bindings(symtab=symtab, let_bindings=script.let or [])

                # THEN: the path rendered in the *host's* format, not POSIX.
                assert str(symtab["built"]) == r"\a\b", (
                    "a script's own `let` is session scope and must render in the "
                    "host's path format"
                )
                # AND: it resolved against the live session symbol table.
                # `Session.WorkingDirectory` is PATH-typed, so under the forced
                # Windows format it renders with backslashes -- while
                # `session.working_directory` is a real path object in the *host
                # OS's* flavour, which is POSIX here and Windows on CI. The claim
                # is *which* path the binding saw, not how it renders, so both
                # sides are compared as paths rather than as text.
                # `PureWindowsPath` is the right parser for the rendered side
                # because the format was forced to Windows; it also accepts `/`
                # as a separator, so a POSIX `working_directory` parses to the
                # same parts. The format claim is the `built` assertion above.
                assert PureWindowsPath(str(symtab["wd"])) == PureWindowsPath(
                    session.working_directory
                )

    def test_a_failing_binding_still_raises(self) -> None:
        """Negative control for the two tests above: the evaluation is real, so a
        broken binding is still an error rather than being silently skipped."""
        script = _expr_step_script(["bad = Undefined.Symbol"])
        with Session(session_id=uuid.uuid4().hex, job_parameter_values={}) as session:
            symtab = _session_symtab(session)

            # WHEN / THEN
            with pytest.raises(ValueError, match="bad"):
                apply_let_bindings(symtab=symtab, let_bindings=script.let or [])
