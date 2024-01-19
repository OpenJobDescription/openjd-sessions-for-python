# Open Job Description - Sessions for Python

Open Job Description is a flexible open specification for defining render jobs which are portable
between studios and render solutions. This package provides a library that can be used to build
a runtime that is able to run Jobs in a
[Session](https://github.com/OpenJobDescription/openjd-specifications/wiki/How-Jobs-Are-Run#sessions)
as defined by Open Job Description.

For more information about Open Job Description and our goals with it, please see the
Open Job Description [Wiki on GitHub](https://github.com/OpenJobDescription/openjd-specifications/wiki).

## Compatibility

This library requires:

1. Python 3.9 or higher;
2. Linux, MacOS, or Windows operating system;
3. On Linux/MacOS:
    * `sudo`
4. On Windows:
    * PowerShell 7+

## Versioning

This package's version follows [Semantic Versioning 2.0](https://semver.org/).

1. The MAJOR version is currently 0.
2. The MINOR version is incremented when backwards incompatible changes are introduced to the public API.
3. The PATCH version is incremented when bug fixes or backwards compatible changes are introduced to the public API.

## Example Usage

### Running a Session

```python
from openjd.model import (
    StepParameterSpaceIterator,
    create_job,
    decode_job_template,
    preprocess_job_parameters
)
from openjd.sessions import (
    LOG,
    ActionState,
    ActionStatus,
    Parameter,
    Session
)
import logging
import os
from pathlib import Path
import sys
from threading import Event

#   Setup
# ========
job_template_path = Path("/absolute/path/to/job/template.json")
job_template = decode_job_template(
    template={
        "name": "DemoJob",
        "specificationVersion": "jobtemplate-2023-09",
        "parameterDefinitions": [
            { "name": "Foo", "type": "INT" }
        ],
        "jobEnvironments": [
            {
                "name": "DemoJobEnv",
                "script": {
                    "actions": {
                        "onEnter": { "command": "python", "args": [ "-c", "print('Entering DemoJobEnv')" ] },
                        "onExit": { "command": "python", "args": [ "-c", "print('Exiting DemoJobEnv')" ] }
                    }
                }
            }
        ],
        "steps": [
            {
                "name": "DemoStep",
                "stepEnvironments": [
                    {
                        "name": "DemoStepEnv",
                        "script": {
                            "actions": {
                        "onEnter": { "command": "python", "args": [ "-c", "print('Entering DemoStepEnv')" ] },
                        "onExit": { "command": "python", "args": [ "-c", "print('Exiting DemoStepEnv')" ] }
                    }
                        }
                    }
                ],
                "parameterSpace": {
                    "taskParameterDefinitions": [
                        { "name": "Bar", "type": "INT", "range": "1-10" }   
                    ]
                },
                "script": {
                    "actions": {
                        "onRun": { "command": "python", "args": [ "-c", "print(r'Foo={{Param.Foo}} Bar={{Task.Param.Bar}}')" ] }
                    }
                }
            }
        ]
    }
)
job_parameters = preprocess_job_parameters(
    job_template=job_template,
    job_parameter_values={
        "Foo": "12"
    },
    job_template_dir=job_template_path,
    current_working_dir=Path(os.getcwd())
)
job = create_job(
    job_template=job_template,
    job_parameter_values=job_parameters
)

# stdout/stderr from the Session's running processes are sent
# to LOG
LOG.addHandler(logging.StreamHandler(stream=sys.stdout))

#   Run the Session
# ======
action_event = Event()
last_status: ActionStatus = None

def action_complete_callback(session_id: str, status: ActionStatus) -> None:
    # This function will be called by the Session when one of the processes
    # that was started has experienced a status change.
    # e.g. Completing as FAILED/SUCCEEDED, or an update to a progress message.
    global last_status
    last_status = status
    if status.state != ActionState.RUNNING:
        action_event.set()

job_parameter_values = [
    Parameter(
        type=param.type,
        name=name,
        value=param.value
    )
    for name,param in job_parameters.items()
]
# Run all tasks in the DemoStep within a Session
with Session(
    session_id="demo",
    job_parameter_values=job_parameter_values,
    callback=action_complete_callback
) as session:
    unwind_session: bool = False
    environment_ids = list[str]()
    step = job.steps[0]
    try:
        def run_environment(env):
            global status
            action_event.clear()
            id = session.enter_environment(environment=env)
            environment_ids.append(id)
            # enter_environment is non-blocking, wait for the process to complete
            action_event.wait()
            if last_status.state in (ActionState.CANCELED, ActionState.FAILED):
                raise RuntimeError("Abnormal exit")
        # Enter each job environment
        for env in job.jobEnvironments:
            run_environment(env)
        # Enter each step environment
        for env in step.stepEnvironments:
            run_environment(env)
        # Run each task in the step
        for task_parameters in StepParameterSpaceIterator(space=step.parameterSpace):
            action_event.clear()
            task_parameter_values = [
                Parameter(
                    name=name,
                    type=param.type,
                    value=param.value
                )
                for name,param in task_parameters.items()
            ]
            session.run_task(
                step_script=step.script,
                task_parameter_values=task_parameter_values
            )
            # run_task is non-blocking, wait for the process to complete
            action_event.wait()
            if last_status.state in (ActionState.CANCELED, ActionState.FAILED):
                raise RuntimeError("Abnormal exit")
    except RuntimeError:
        pass
    finally:
        # Exit all environments in the reverse order that they were entered.
        environment_ids.reverse()
        for id in environment_ids:
            session.exit_environment(identifier=id)
            action_event.clear()
            action_event.wait()
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
