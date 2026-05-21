# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""YAML-based session scenario tests.

Each scenario YAML file references a standalone job template that can be run
independently with `openjd run`, plus test-specific parameters and expectations.

Scenario YAML Format
--------------------

```yaml
name: "Human-readable test name"
description: "What this scenario tests"

# Platform restriction (optional, default: all)
# Values: all, windows, posix
run_on: posix

# Path to job template file (required)
job_template_file: "my_template.yaml"

# Job parameter values (optional)
job_parameters:
  ParamName: "value"
  ListParam: ["a", "b"]

# Path mapping rules (optional)
path_mapping_rules:
  - source_path_format: windows
    source_path: "C:\\Users"
    destination_path: "/home"

# Expected behavior (optional)
expect:
  success: true  # default: true
  output_contains:
    - "pattern that must appear in output"
  output_excludes:
    - "pattern that must NOT appear"

# Platform-specific expectations (optional, merged into expect)
expect_posix:
  output_contains:
    - "posix-only pattern"
expect_windows:
  output_contains:
    - "windows-only pattern"
```

File Naming Convention
----------------------
- Scenario files must end with `_scenario.yaml`
- Template files typically end with `_template.yaml`
- Templates can be run standalone: `openjd run my_template.yaml`
"""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from openjd.model._v1 import (
    create_job,
    decode_job_template,
)
from openjd.model._v1.types import (
    JobParameterType,
    JobParameterValue,
)
from openjd.model._v1.job import (
    StepParameterSpaceIterator,
)
from openjd.sessions._v1 import Session, PathMappingRule, SessionState


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def discover_scenarios() -> list[Path]:
    """Find all scenario YAML files (ending with _scenario.yaml)."""
    if not SCENARIOS_DIR.exists():
        return []
    return sorted(SCENARIOS_DIR.rglob("*_scenario.yaml"))


def should_run(scenario: dict[str, Any]) -> bool:
    """Check if scenario should run on current platform based on run_on."""
    run_on = scenario.get("run_on", "all")
    if run_on not in ("all", "windows", "posix"):
        raise ValueError(f"Invalid run_on '{run_on}'. Must be 'all', 'windows', or 'posix'")
    if run_on == "all":
        return True
    is_windows = os.name == "nt"
    if run_on == "windows":
        return is_windows
    if run_on == "posix":
        return not is_windows
    return True


def parse_path_mapping_rules(rules_data: list[dict]) -> list[PathMappingRule]:
    """Convert YAML path mapping rules to PathMappingRule objects."""
    return [PathMappingRule.from_dict(r) for r in rules_data]


def build_parameter_values(
    params: dict[str, Any], job_template: Any
) -> dict[str, JobParameterValue]:
    """Build JobParameterValue dict from scenario parameters."""
    # Get type info from template's parameter definitions. The v1
    # binding exposes `JobParameterType` as a pyclass enum without
    # a value-from-string constructor; pass the variant directly.
    # `.name` is the identifier (e.g. "STRING", "LIST_PATH"),
    # `.as_str()` is the spec form (e.g. "STRING", "LIST[PATH]")
    # — different shapes, so we hold onto the variant itself.
    type_map: dict[str, JobParameterType] = {}
    for param_def in getattr(job_template, "parameterDefinitions", []) or []:
        type_map[param_def.name] = param_def.type

    result = {}
    for name, value in params.items():
        ptype = type_map.get(name, JobParameterType.STRING)
        result[name] = JobParameterValue(type=ptype, value=value)
    return result


# Session states the runner waits to settle into between actions.
# In the v1 binding `SessionState` is a pyclass enum (no `.value`
# attribute); compare against the enum variants directly.
_QUIESCENT_STATES = {
    SessionState.READY,
    SessionState.ENDED,
    SessionState.READY_ENDING,
}


class TestSessionScenarios:
    """Run YAML-defined session scenarios."""

    @pytest.mark.parametrize(
        "scenario_path",
        discover_scenarios(),
        ids=lambda p: str(p.relative_to(SCENARIOS_DIR)).replace("/", "::"),
    )
    def test_scenario(self, scenario_path: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        """Execute a single scenario and verify expectations."""
        scenario = yaml.safe_load(scenario_path.read_text())

        # Check platform
        if not should_run(scenario):
            run_on = scenario.get("run_on", "all")
            pytest.skip(f"Scenario only runs on {run_on}")

        # Load referenced template
        template_file = scenario.get("job_template_file")
        if template_file:
            template_path = scenario_path.parent / template_file
            template_dict = yaml.safe_load(template_path.read_text())
        else:
            pytest.fail("Scenario must specify job_template_file")

        # Decode template
        extensions = template_dict.get("extensions", [])
        job_template = decode_job_template(template=template_dict, supported_extensions=extensions)

        # Build parameter values
        job_params = build_parameter_values(scenario.get("job_parameters", {}), job_template)

        # Build path mapping rules
        path_rules = parse_path_mapping_rules(scenario.get("path_mapping_rules", []))

        # Create job to get step script
        job = create_job(job_template=job_template, job_parameter_values=job_params)

        # Select step (by name or default to first)
        step_name = scenario.get("step")
        if step_name:
            step = next((s for s in job.steps if s.name == step_name), None)
            if not step:
                pytest.fail(f"Step '{step_name}' not found in template")
        else:
            step = job.steps[0]
        step_script = step.script

        # Track environment identifiers for exit
        job_env_ids = []
        step_env_ids = []

        # Run session and iterate through parameter space
        with Session(
            session_id="scenario-test",
            job_parameter_values=job_params,
            path_mapping_rules=path_rules if path_rules else None,
            session_root_directory=tmp_path,
        ) as session:
            # Enter job environments
            for env in job.jobEnvironments or []:
                env_id = session.enter_environment(environment=env)
                job_env_ids.append(env_id)
                while session.state not in _QUIESCENT_STATES:
                    import time

                    time.sleep(0.01)

            # Enter step environments
            for env in step.stepEnvironments or []:
                env_id = session.enter_environment(
                    environment=env,
                    resolved_symtab=step.resolved_symtab,
                )
                step_env_ids.append(env_id)
                while session.state not in _QUIESCENT_STATES:
                    import time

                    time.sleep(0.01)

            # Run tasks
            for task_params in StepParameterSpaceIterator(space=step.parameterSpace):
                session.run_task(
                    step_script=step_script,
                    task_parameter_values=task_params,
                    resolved_symtab=step.resolved_symtab,
                )
                while session.state not in _QUIESCENT_STATES:
                    import time

                    time.sleep(0.01)

            # Exit step environments (reverse order)
            for env_id in reversed(step_env_ids):
                session.exit_environment(
                    identifier=env_id,
                    resolved_symtab=step.resolved_symtab,
                )
                while session.state not in _QUIESCENT_STATES:
                    import time

                    time.sleep(0.01)

            # Exit job environments (reverse order)
            for env_id in reversed(job_env_ids):
                session.exit_environment(identifier=env_id)
                while session.state not in _QUIESCENT_STATES:
                    import time

                    time.sleep(0.01)

        # Collect output from logs
        captured_output = caplog.messages

        # Verify expectations
        expect = scenario.get("expect", {})

        # Merge platform-specific expectations
        is_windows = os.name == "nt"
        platform_expect = scenario.get("expect_windows" if is_windows else "expect_posix", {})
        # Merge platform-specific output_contains/output_excludes into main expect
        for key in ("output_contains", "output_excludes"):
            if key in platform_expect:
                expect.setdefault(key, []).extend(platform_expect[key])

        if expect.get("success", True):
            # Check for failure indicators in output
            for msg in captured_output:
                assert "openjd_fail:" not in msg.lower(), f"Unexpected failure: {msg}"

        for pattern in expect.get("output_contains", []):
            found = any(pattern in msg for msg in captured_output)
            assert found, f"Expected output containing '{pattern}' not found in: {captured_output}"

        for pattern in expect.get("output_excludes", []):
            found = any(pattern in msg for msg in captured_output)
            assert not found, f"Unexpected output containing '{pattern}' found"
