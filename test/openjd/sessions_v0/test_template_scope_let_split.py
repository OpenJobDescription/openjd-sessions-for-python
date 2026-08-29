# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""A step script's merged ``let`` list spans two scopes; each half must be
evaluated in its own path format.

An instantiated Step's ``script.let`` is ``step-level bindings + the script's
own``, in that order. The step-level prefix was already evaluated at job
creation in *template* scope, which openjd-rs (and now openjd-model) evaluate
with ``PathFormat::Posix`` so a create-time value cannot depend on the host that
created the job. Re-evaluating that prefix at session time in the host's format
re-renders its PATH values -- on Windows ``path("/foo/bar")`` becomes
``\\foo\\bar``, so ``startswith(path("/foo/bar"), "/foo")`` flips from ``true``
to ``false`` and the same binding holds a different value in the two
evaluations. That is what broke 11 conformance fixtures on the Python-on-Windows
CI leg.

``apply_script_let_bindings`` owns the split. The script's own bindings are
session scope and keep the host's format, because they legitimately reference
``Session.WorkingDirectory``, ``Task.File.*`` and ``apply_path_mapping``.

The claim is narrow, and ``TestPrefixScopeIsNarrowedToFormatNeutralSymbols``
is where the boundary is drawn. Template scope is POSIX, so the prefix is
evaluated against only the symbols in scope that carry no path format. A prefix
binding that needs one -- a PATH job parameter, ``Session.WorkingDirectory``, a
create-time value seeded natively -- cannot be reproduced here at all, and the
whole list falls back to a single host-format evaluation instead: the previous
behaviour, still wrong on Windows for that script, but never raising and never
reading a path under a format it was not built in.

Note on what these tests can and cannot prove. The path format handed to each
half is asserted directly, by spying on the model's ``evaluate_let_bindings``,
rather than by comparing rendered values. On a POSIX host the host format *is*
POSIX, so a value comparison cannot distinguish "POSIX because we asked for it"
from "POSIX because that is the host" -- it would pass on this host no matter
what the code did. ``test_path_format_is_load_bearing`` supplies the missing
anchor: it shows a non-POSIX format really does change rendering, so the
argument these tests assert on is the argument that matters. The Windows
behaviour itself is not exercised here.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch as mock_patch

import pytest

from openjd.model import SymbolTable, evaluate_let_bindings
from openjd.model.v2023_09 import (
    ModelParsingContext as ModelParsingContext_2023_09,
    StepScript as StepScript_2023_09,
)
from openjd.sessions import ActionState, Session, SessionState
from openjd.sessions._embedded_files import EmbeddedFilesScope
from openjd.sessions._runner_base import apply_let_bindings, apply_script_let_bindings
from openjd.sessions._runner_step_script import StepScriptRunner

from .conftest import build_logger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_A_PATH_BINDING = "path('/foo/bar')"
"""A binding RHS whose value renders differently per path format, which is the
whole reason the split exists."""


class _FakeScript:
    """Stands in for an instantiated ``StepScript``.

    A fake rather than a real model object because these tests are about the
    *boundary index*, and the model only produces indices its own templates can
    express. Reading the count off a plain attribute is exactly what the
    ``getattr`` in the helper does, and the real-model wiring is pinned
    separately by :class:`TestStepScriptWiring`.
    """

    def __init__(self, count: int) -> None:
        self._template_scope_let_count = count


class _NoCountScript:
    """An openjd-model that predates the model-side half of the fix: no
    ``_template_scope_let_count`` attribute at all."""


def _set_count(script: Any, count: int) -> None:
    """Set the template-scope boundary on a real model object.

    ``setattr`` rather than a direct assignment because openjd-sessions builds
    against ``openjd-model >= 0.11.6``, which does not declare the private
    attribute -- a direct assignment fails ``hatch run typing`` against the
    declared floor. Reaching it through ``setattr`` keeps the tests type-clean
    on both model versions, which is the same reason the helper under test
    reads it through ``getattr``.
    """
    setattr(script, "_template_scope_let_count", count)


def _spy_on_evaluation():
    """Patch the model's ``evaluate_let_bindings`` where openjd-sessions imports
    it, recording every call while still evaluating for real.

    Spying here rather than on ``apply_let_bindings`` keeps the
    ``MAX_LET_BINDING_LENGTH`` guard and the real evaluation in the loop, so a
    test can assert both the calls and the resulting symbol values.
    """
    return mock_patch(
        "openjd.sessions._runner_base.evaluate_let_bindings",
        side_effect=evaluate_let_bindings,
    )


def _calls(spy: Any) -> list[tuple[list[str], Any]]:
    """The spy's calls as ``[(let_bindings, path_format), ...]``.

    ``path_format`` is read with ``.get`` because ``apply_let_bindings`` omits
    the kwarg entirely for the host format -- it does not exist on openjd-model
    at this package's declared floor. Omitted and ``None`` are the same request
    (the engine's default, i.e. the host's format), so both read as ``None``
    here.
    """
    return [
        (call.kwargs["let_bindings"], call.kwargs.get("path_format")) for call in spy.call_args_list
    ]


def _posix_format() -> Any:
    from openjd.expr import PathFormat

    return PathFormat.POSIX


def _step_script(
    let: list[str], command: str = "echo", args: Optional[list[str]] = None
) -> StepScript_2023_09:
    """A real ``StepScript`` carrying ``let``. The merged-list *boundary* is set
    by the caller via ``_template_scope_let_count``, because building it through
    ``create_job`` would drag a whole job template into a test about one
    index."""
    context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
    return StepScript_2023_09.model_validate(
        {"let": let, "actions": {"onRun": {"command": command, "args": args or ["ok"]}}},
        context=context,
    )


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestApplyScriptLetBindings:
    def test_splits_at_the_template_scope_count(self) -> None:
        # GIVEN: four bindings, of which the first two are step level.
        bindings = ["a = 1", "b = 2", "c = 3", "d = 4"]
        symtab = SymbolTable()

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(2))

        # THEN: exactly two evaluations, split at index 2, prefix first.
        assert _calls(spy) == [
            (["a = 1", "b = 2"], _posix_format()),
            (["c = 3", "d = 4"], None),
        ]
        # ...and both halves landed in the SAME table.
        assert [str(symtab[name]) for name in ("a", "b", "c", "d")] == ["1", "2", "3", "4"]

    def test_prefix_evaluates_posix_and_suffix_evaluates_host_format(self) -> None:
        # GIVEN
        bindings = [f"tmpl = {_A_PATH_BINDING}", f"own = {_A_PATH_BINDING}"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=SymbolTable(), let_bindings=bindings, script=_FakeScript(1)
            )

        # THEN: the template-scope half is pinned to POSIX; the session-scope
        # half is left at the engine default, which is the host's format.
        prefix, suffix = _calls(spy)
        assert prefix == ([f"tmpl = {_A_PATH_BINDING}"], _posix_format())
        assert suffix == ([f"own = {_A_PATH_BINDING}"], None)

    def test_path_format_is_load_bearing(self) -> None:
        """The anchor for the assertions above: a path format other than POSIX
        really does change how a PATH value renders, so forwarding the argument
        is not cosmetic. Without this, a mutant that passed the host format for
        the prefix would only be caught by an argument comparison that could
        itself be dismissed as testing the mock."""
        from openjd.expr import PathFormat

        posix, windows = SymbolTable(), SymbolTable()

        # WHEN
        apply_let_bindings(
            symtab=posix, let_bindings=[f"p = {_A_PATH_BINDING}"], path_format=PathFormat.POSIX
        )
        apply_let_bindings(
            symtab=windows, let_bindings=[f"p = {_A_PATH_BINDING}"], path_format=PathFormat.WINDOWS
        )

        # THEN
        assert str(posix["p"]) == "/foo/bar"
        assert str(windows["p"]) == "\\foo\\bar"
        # And the flip that broke the fixtures, reproduced without a Windows host.
        assert str(posix["p"]).startswith("/foo")
        assert not str(windows["p"]).startswith("/foo")

    def test_no_template_scope_prefix_behaves_exactly_as_before(self) -> None:
        # GIVEN: a script with only its own bindings -- count 0.
        bindings = ["a = 1", "b = 2"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=SymbolTable(), let_bindings=bindings, script=_FakeScript(0)
            )

        # THEN: one evaluation, host format, whole list. No POSIX evaluation at
        # all -- a count of 0 must not produce an empty extra call.
        assert _calls(spy) == [(bindings, None)]

    def test_missing_count_attribute_falls_back_to_host_format(self) -> None:
        """An openjd-model without the model-side half of the fix must degrade
        to the previous behaviour, not raise."""
        # GIVEN
        bindings = ["a = 1", "b = 2"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=SymbolTable(), let_bindings=bindings, script=_NoCountScript()
            )

        # THEN
        assert _calls(spy) == [(bindings, None)]

    def test_no_script_falls_back_to_host_format(self) -> None:
        """What an environment script's caller passes: its own bindings are
        session scope and correctly use the host format."""
        # GIVEN
        bindings = ["a = 1"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=SymbolTable(), let_bindings=bindings)

        # THEN
        assert _calls(spy) == [(bindings, None)]

    def test_count_beyond_the_list_falls_back_to_session_scope(self) -> None:
        """A model/sessions version skew reporting a boundary the list cannot
        have is not guessed at.

        Clamping to the list length was the earlier behaviour and it is worse:
        it would evaluate a genuinely session-scope binding in template scope.
        Falling back to 0 is the pre-fix behaviour, which is wrong on Windows
        but never raises and never mis-scopes a binding."""
        # GIVEN
        bindings = ["a = 1"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=SymbolTable(), let_bindings=bindings, script=_FakeScript(5)
            )

        # THEN: one evaluation, host format, whole list.
        assert _calls(spy) == [(bindings, None)]

    def test_negative_count_falls_back_to_session_scope(self) -> None:
        """Same guard, other side: a negative boundary is impossible."""
        # GIVEN
        bindings = ["a = 1", "b = 2"]

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=SymbolTable(), let_bindings=bindings, script=_FakeScript(-1)
            )

        # THEN
        assert _calls(spy) == [(bindings, None)]

    def test_ordering_is_preserved_across_the_boundary(self) -> None:
        """A script-level binding may reference a step-level one, so the prefix
        must be evaluated -- into the same table -- before the suffix."""
        # GIVEN: `under` is session scope and reads `root`, which is template
        # scope.
        bindings = [f"root = {_A_PATH_BINDING}", "under = startswith(root, '/foo')"]
        symtab = SymbolTable()

        # WHEN
        apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(1))

        # THEN
        assert str(symtab["root"]) == "/foo/bar"
        assert str(symtab["under"]) == "true"

    def test_a_failing_suffix_binding_still_raises(self) -> None:
        """The split must not swallow an evaluation error in either half."""
        # WHEN / THEN
        with pytest.raises(ValueError, match="let binding 'bad'"):
            apply_script_let_bindings(
                symtab=SymbolTable(),
                let_bindings=["ok = 1", "bad = NoSuchSymbol"],
                script=_FakeScript(1),
            )


# ---------------------------------------------------------------------------
# The narrowed prefix scope, and the all-or-nothing fallback
# ---------------------------------------------------------------------------


class TestPrefixScopeIsNarrowedToFormatNeutralSymbols:
    """Template scope is POSIX, so the prefix cannot read a session symbol that
    carries the host's path format.

    Reading one either raises ``Path format mismatch`` or -- worse -- silently
    succeeds against a re-rendered value: ``.parent`` of a Windows path read as
    POSIX is ``'.'``, because a backslash is an ordinary POSIX path character.
    So those symbols are not in scope for the prefix, and a prefix that needs one
    abandons the split for the whole list.

    These assertions are host-independent: the filter removes the symbol on
    either host, so the binding fails with ``Undefined variable`` on both.
    """

    @staticmethod
    def _session_shaped_symtab() -> SymbolTable:
        """A symbol table with one symbol of each shape the session seeds."""
        symtab = SymbolTable()
        symtab["Job.Name"] = "a-job"
        symtab["Param.S"] = "text"
        symtab.expr_types["Param.S"] = "STRING"
        symtab["Param.N"] = "3"
        symtab.expr_types["Param.N"] = "INT"
        symtab["Param.Out"] = "/mnt/out"
        symtab.expr_types["Param.Out"] = "PATH"
        symtab["Param.Ins"] = ["/mnt/a", "/mnt/b"]
        symtab.expr_types["Param.Ins"] = "LIST[PATH]"
        symtab["Session.WorkingDirectory"] = "/sessions/s1"
        symtab.expr_types["Session.WorkingDirectory"] = "PATH"
        return symtab

    @staticmethod
    def _unsplit(symtab: SymbolTable, bindings: list[str]) -> SymbolTable:
        """The pre-fix behaviour: the whole list, once, in the host's format."""
        before = SymbolTable(source=symtab)
        apply_let_bindings(symtab=before, let_bindings=bindings)
        return before

    def _assert_fell_back(self, bindings: list[str], count: int) -> None:
        """The prefix was attempted in POSIX, declined, and the WHOLE list was
        then evaluated once in the host's format -- with the pre-fix values."""
        symtab = self._session_shaped_symtab()
        expected = self._unsplit(symtab, bindings)

        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(
                symtab=symtab, let_bindings=bindings, script=_FakeScript(count)
            )

        assert _calls(spy) == [(bindings[:count], _posix_format()), (bindings, None)]
        bound = [b.partition("=")[0].strip() for b in bindings]
        assert [str(symtab[n]) for n in bound] == [str(expected[n]) for n in bound]

    def test_a_self_contained_prefix_binding_still_freezes(self) -> None:
        """The case the fix exists for is untouched: a prefix that reads nothing
        format-carrying is still evaluated in template scope."""
        # GIVEN
        bindings = [f"tmpl = string({_A_PATH_BINDING})", "own = 1"]
        symtab = self._session_shaped_symtab()

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(1))

        # THEN: still split, and the value froze the POSIX text on either host.
        assert _calls(spy) == [([bindings[0]], _posix_format()), (["own = 1"], None)]
        assert str(symtab["tmpl"]) == "/foo/bar"

    def test_a_format_neutral_symbol_is_readable_from_the_prefix(self) -> None:
        """The filter is a *shape* test, not a name denylist: a STRING or INT
        parameter and ``Job.Name`` carry no path format, so they stay in scope
        and do not trigger the fallback.

        ``Param.N * 2`` is 6 only if the symbol's declared INT type came across
        with it; an untyped ``"3"`` would make it the string ``"33"``."""
        # GIVEN
        bindings = ["label = join([Job.Name, Param.S], '-')", "doubled = Param.N * 2"]
        symtab = self._session_shaped_symtab()

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(2))

        # THEN: one POSIX evaluation of the whole prefix, and no fallback call.
        assert _calls(spy) == [(bindings, _posix_format())]
        assert str(symtab["label"]) == "a-job-text"
        assert str(symtab["doubled"]) == "6"

    def test_a_prefix_binding_reading_a_path_parameter_falls_back(self) -> None:
        # GIVEN: `Param.Out` is a host-format PATH.
        self._assert_fell_back(["out = string(Param.Out)", "own = 1"], count=1)

    def test_a_prefix_binding_reading_a_list_path_parameter_falls_back(self) -> None:
        # GIVEN: LIST[PATH] carries a format as much as PATH does.
        self._assert_fell_back(["ins = string(Param.Ins[0])", "own = 1"], count=1)

    def test_a_prefix_binding_reading_the_session_working_directory_falls_back(self) -> None:
        # GIVEN: the symbol whose `.parent` silently yields '.' on Windows.
        self._assert_fell_back(["under = string(Session.WorkingDirectory.parent)"], count=1)

    @pytest.mark.parametrize(
        "base_rhs, read",
        [
            pytest.param(_A_PATH_BINDING, "base", id="path"),
            pytest.param(f"[{_A_PATH_BINDING}, path('/a')]", "base[0]", id="list[path]"),
        ],
    )
    def test_a_prefix_binding_reading_a_native_path_value_falls_back(
        self, base_rhs: str, read: str
    ) -> None:
        """The other half of the filter. A create-time table reaches the session
        as native engine values (``Session._resolved_base_entries``), so a
        path-typed one carries its format in the value itself with no
        ``expr_types`` entry to declare it. A native ``list[path]`` carries one
        just as much, one type parameter down."""
        # GIVEN: `base` in the shape `_resolved_base_entries` produces -- a
        # native path value tagged with the host's format.
        seed = SymbolTable()
        apply_let_bindings(symtab=seed, let_bindings=[f"base = {base_rhs}"])
        symtab = self._session_shaped_symtab()
        symtab["base"] = seed["base"]
        assert "base" not in symtab.expr_types
        bindings = [f"out = string({read})", "own = 1"]
        expected = self._unsplit(symtab, bindings)

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(1))

        # THEN
        assert _calls(spy) == [([bindings[0]], _posix_format()), (bindings, None)]
        assert str(symtab["out"]) == str(expected["out"])

    def test_the_fallback_leaves_no_partial_prefix_behind(self) -> None:
        """All-or-nothing. A prefix binding that succeeded in POSIX before a
        later one declined must not be seeded: it would leave a POSIX-evaluated
        value for a host-evaluated sibling to read."""
        # GIVEN: `first` evaluates fine in POSIX; `second` needs a PATH param.
        bindings = [f"first = string({_A_PATH_BINDING})", "second = string(Param.Out)"]
        symtab = self._session_shaped_symtab()
        expected = self._unsplit(symtab, bindings)

        # WHEN
        with _spy_on_evaluation() as spy:
            apply_script_let_bindings(symtab=symtab, let_bindings=bindings, script=_FakeScript(2))

        # THEN: both names hold the host-format value, not the POSIX one.
        assert _calls(spy) == [(bindings, _posix_format()), (bindings, None)]
        assert str(symtab["first"]) == str(expected["first"])
        assert str(symtab["second"]) == str(expected["second"])

    def test_a_failing_prefix_binding_still_raises_through_the_fallback(self) -> None:
        """The fallback must not turn a genuine error into silence. Evaluating
        the whole list in the host's format re-raises it -- which is exactly what
        the pre-fix code did."""
        # WHEN / THEN
        with pytest.raises(ValueError, match="let binding 'bad'"):
            apply_script_let_bindings(
                symtab=self._session_shaped_symtab(),
                let_bindings=["bad = NoSuchSymbol", "own = 1"],
                script=_FakeScript(1),
            )


# ---------------------------------------------------------------------------
# The wiring: the runner and the RFC 0008 wrapped-inner-scope path
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestStepScriptWiring:
    """The helper is only useful if the sites that evaluate a step script's
    merged ``let`` actually hand it the script."""

    def _run(
        self,
        queue_handler: Any,
        session_dir: Path,
        script: StepScript_2023_09,
        count: int,
    ) -> list[tuple[list[str], Any]]:
        _set_count(script, count)
        # `with runner:` rather than `with StepScriptRunner(...) as runner:` --
        # ScriptRunnerBase.__enter__ is annotated as returning the base class, so
        # the `as` form loses the subclass and with it `run()`.
        runner = StepScriptRunner(
            logger=build_logger(queue_handler),
            script=script,
            symtab=SymbolTable(),
            session_working_directory=session_dir,
            session_files_directory=session_dir,
        )
        with runner:
            with _spy_on_evaluation() as spy:
                runner.run()
            deadline = time.time() + 20
            while runner.state.value == "running" and time.time() < deadline:
                time.sleep(0.05)
            return _calls(spy)

    def test_step_runner_splits_its_merged_let(
        self, queue_handler: Any, tmp_path: Path, python_exe: str
    ) -> None:
        # GIVEN: a step script whose first binding is step level.
        script = _step_script(
            [f"tmpl = {_A_PATH_BINDING}", "own = 1"],
            command=python_exe,
            args=["-c", "pass"],
        )

        # WHEN
        calls = self._run(queue_handler, tmp_path, script, count=1)

        # THEN
        assert calls == [
            ([f"tmpl = {_A_PATH_BINDING}"], _posix_format()),
            (["own = 1"], None),
        ]

    def test_step_runner_with_embedded_files_splits_its_merged_let(
        self, queue_handler: Any, tmp_path: Path, python_exe: str
    ) -> None:
        """The embedded-files branch evaluates the same merged list through
        ``_materialize_files``, so it needs the same split. Missing this leaves
        the bug live for any step that has both step-level bindings and
        embedded files."""
        # GIVEN
        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        script = StepScript_2023_09.model_validate(
            {
                "let": [f"tmpl = {_A_PATH_BINDING}", "own = 1"],
                "embeddedFiles": [{"name": "F", "type": "TEXT", "data": "{{ tmpl }}"}],
                "actions": {"onRun": {"command": python_exe, "args": ["-c", "pass"]}},
            },
            context=context,
        )

        # WHEN
        calls = self._run(queue_handler, tmp_path, script, count=1)

        # THEN
        assert calls == [
            ([f"tmpl = {_A_PATH_BINDING}"], _posix_format()),
            (["own = 1"], None),
        ]

    def test_wrapped_inner_scope_splits_a_step_scripts_merged_let(self) -> None:
        """RFC 0008: the wrapped action's scope is rebuilt from the inner
        script's ``let``, so it must be split the same way -- otherwise a
        wrapped action resolves against a scope that differs from the one it
        would have had unwrapped."""
        # GIVEN
        script = _step_script([f"tmpl = {_A_PATH_BINDING}", "own = 1"])
        _set_count(script, 1)
        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})

        # WHEN
        try:
            with _spy_on_evaluation() as spy:
                inner = session._build_wrapped_inner_scope(
                    EmbeddedFilesScope.STEP, script.let, None, SymbolTable(), script
                )
        finally:
            session.cleanup()

        # THEN
        assert _calls(spy) == [
            ([f"tmpl = {_A_PATH_BINDING}"], _posix_format()),
            (["own = 1"], None),
        ]
        assert str(inner["tmpl"]) == "/foo/bar"

    def test_environment_script_bindings_stay_host_format(
        self, queue_handler: Any, tmp_path: Path, python_exe: str
    ) -> None:
        """The other half of the contract: an environment script's own ``let``
        is session scope. Nothing about it may change."""
        from openjd.model.v2023_09 import EnvironmentScript as EnvironmentScript_2023_09
        from openjd.sessions._runner_env_script import EnvironmentScriptRunner

        # GIVEN
        context = ModelParsingContext_2023_09(supported_extensions=["EXPR"])
        env_script = EnvironmentScript_2023_09.model_validate(
            {
                "let": [f"a = {_A_PATH_BINDING}", "b = 1"],
                "actions": {"onEnter": {"command": python_exe, "args": ["-c", "pass"]}},
            },
            context=context,
        )

        # WHEN
        runner = EnvironmentScriptRunner(
            logger=build_logger(queue_handler),
            environment_script=env_script,
            symtab=SymbolTable(),
            session_working_directory=tmp_path,
            session_files_directory=tmp_path,
        )
        with runner:
            with _spy_on_evaluation() as spy:
                runner.enter()
            deadline = time.time() + 20
            while runner.state.value == "running" and time.time() < deadline:
                time.sleep(0.05)
            calls = _calls(spy)

        # THEN: one evaluation, host format, whole list.
        assert calls == [([f"a = {_A_PATH_BINDING}", "b = 1"], None)]


@pytest.mark.usefixtures("message_queue", "queue_handler")
class TestEndToEndScopeAgreement:
    """The property the conformance fixtures actually check: the value a
    step-level binding holds at session time equals the value it held at job
    creation."""

    def test_a_step_level_path_binding_agrees_across_the_two_evaluations(
        self, python_exe: str
    ) -> None:
        # GIVEN: a step script whose step-level prefix is a path predicate --
        # the shape that flipped on Windows.
        script = _step_script(
            [f"root = {_A_PATH_BINDING}", "under = startswith(root, '/foo')"],
            command=python_exe,
        )
        _set_count(script, 2)

        # AND: the value the model computed at job creation, in template scope.
        create_time = SymbolTable()
        apply_let_bindings(
            symtab=create_time, let_bindings=script.let or [], path_format=_posix_format()
        )

        # WHEN: the session re-evaluates the same list.
        session_time = SymbolTable()
        apply_script_let_bindings(symtab=session_time, let_bindings=script.let or [], script=script)

        # THEN: the two agree. On a POSIX host they would agree either way; the
        # per-half format assertions above are what make this host-independent.
        assert str(session_time["root"]) == str(create_time["root"])
        assert str(session_time["under"]) == str(create_time["under"]) == "true"

    def test_a_session_runs_a_step_with_a_step_level_path_binding(self, python_exe: str) -> None:
        """End to end through the public API, so the split cannot break the
        ordinary run."""
        # GIVEN: the action's exit status is driven by the step-level binding's
        # value, so a scope disagreement fails the action rather than passing
        # quietly.
        script = _step_script(
            [f"root = {_A_PATH_BINDING}", "under = startswith(root, '/foo')"],
            command=python_exe,
            args=["-c", "import sys; sys.exit(0 if sys.argv[1] == 'true' else 1)", "{{ under }}"],
        )
        # count=2, so BOTH bindings are template scope. With count=1, `under`
        # would be session scope, and on a Windows host it reads `root` in host
        # format and evaluates to `false` -- correct behaviour, but it would make
        # this assertion fail there while passing on POSIX. The step-level pair
        # is what this test is about, so both belong in the prefix.
        _set_count(script, 2)

        session = Session(session_id=uuid.uuid4().hex, job_parameter_values={})
        try:
            # WHEN
            session.run_task(step_script=script, task_parameter_values={})
            deadline = time.time() + 20
            while session.state == SessionState.RUNNING and time.time() < deadline:
                time.sleep(0.05)

            # THEN
            status = session.action_status
            assert status is not None and status.state == ActionState.SUCCESS, status
        finally:
            session.cleanup()
