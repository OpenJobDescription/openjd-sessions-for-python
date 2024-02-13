# Open Job Description - Sessions for Python

[![pypi](https://img.shields.io/pypi/v/openjd-sessions.svg)](https://pypi.python.org/pypi/openjd-sessions)

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
    * PowerShell 5.x

**EXPERIMENTAL** Note that compatibility with the Windows operating system is currently in active development
and should be considered to be experimental. We recommend that this library not be used in Windows-based
production environments at this time. We will remove this notice when Windows compatibility is considered
sufficiently stable and secure for use in Windows-based production environments.

## Versioning

This package's version follows [Semantic Versioning 2.0](https://semver.org/), but is still considered to be in its 
initial development, thus backwards incompatible versions are denoted by minor version bumps. To help illustrate how
versions will increment during this initial development stage, they are described below:

1. The MAJOR version is currently 0, indicating initial development. 
2. The MINOR version is currently incremented when backwards incompatible changes are introduced to the public API. 
3. The PATCH version is currently incremented when bug fixes or backwards compatible changes are introduced to the public API.

## Contributing

We encourage all contributions to this package.  Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please see [CONTRIBUTING.md](./CONTRIBUTING.md) for our contributing guidelines.

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
    Session
)
import logging
import os
from pathlib import Path
import sys
from threading import Event

#   Setup
# ========
job_template_path = Path("/absolute/path/to/job")
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

# stdout/stderr from the Session's running processes are sent to LOG
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

# Run all tasks in the DemoStep within a Session
with Session(
    session_id="demo",
    job_parameter_values=job_parameters,
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
            session.run_task(
                step_script=step.script,
                task_parameter_values=task_parameters
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
        for _id in environment_ids:
            session.exit_environment(identifier=_id)
            action_event.clear()
            action_event.wait()
```

## Downloading

You can download this package from:
- [PyPI](https://pypi.org/project/openjd-sessions/)
- [GitHub releases](https://github.com/OpenJobDescription/openjd-sessions-for-python/releases)

### Verifying GitHub Releases

You can verify the authenticity of the release artifacts using the `gpg` command line tool.

1) Download the desired release artifacts from the GitHub releases page. Make sure to download the corresponding PGP signature file (ending with `.sig`) as well.
For example, if you would like to verify your download of the wheel for version `1.2.3`, you should have the following files downloaded:
    ```
    openjd_sessions-1.2.3-py3-none-any.whl
    openjd_sessions-1.2.3-py3-none-any.whl.sig
    ```

2) Install the `gpg` command line tool. The installation process varies by operating system. Please refer to the GnuPG website for instructions: https://gnupg.org/download/

3) Save the following contents to a file called `openjobdescription-pgp.asc`:
    ```
    -----BEGIN PGP PUBLIC KEY BLOCK-----
    
    mQINBGXGjx0BEACdChrQ/nch2aYGJ4fxHNQwlPE42jeHECqTdlc1V/mug+7qN7Pc
    C4NQk4t68Y72WX/NG49gRfpAxPlSeNt18c3vJ9/sWTukmonWYGK0jQGnDWjuVgFT
    XtvJAAQBFilQXN8h779Th2lEuD4bQX+mGB7l60Xvh7vIehE3C4Srbp6KJXskPLPo
    dz/dx7a+GXRiyYCYbGX4JziXSjQZRc0tIaxLn/GDm7VnXpdHcUk3qJitree61oC8
    agtRHCH5s56E8wt8fXzyStElMkFIZsoLDlLp5lFqT81En9ho/+K6RLBkIj0mC8G7
    BafpHKlxkrIgNK3pWACL93GE6xihqwkZMCAeqloVvkOTdfAKDHuDSEHwKxHG3cZ1
    /e1YhtkPMVF+NMeoQavykUGVUT1bRoVNdk6bYsnbUjUI1A+JNf6MqvdRJyckZqEC
    ylkBekBp/SFpFHvQkRCpfVizm2GSrjdZKgXpm1ZlQJyMRVzc/XPbqdSWhz52r3IC
    eudwReHDc+6J5rs6tg3NbFfPVfCBMSqHlu1HRewWAllIp1+y6nfL4U3iEsUvZ1Y6
    IV3defHIP3kNPU14ZWf3G5rvJDZrIRnjoWhDcaVmivmB/cSdDzphL5FovSI8dsPm
    iU/JZGQb3EvZq+nl4pOiK32hETJ/fgCCzgUA3WqGeFNUNSI9KYZgBe6daQARAQAB
    tDRPcGVuIEpvYiBEZXNjcmlwdGlvbiA8b3BlbmpvYmRlc2NyaXB0aW9uQGFtYXpv
    bi5jb20+iQJXBBMBCABBFiEEvBcWYrv5OB7Tl2sZovDwWbzECYcFAmXGjx0DGy8E
    BQkDwmcABQsJCAcCAiICBhUKCQgLAgMWAgECHgcCF4AACgkQovDwWbzECYcSHRAA
    itPYx48xnJiT6tfnult9ZGivhcXhrMlvirVYOqEtRrt0l18sjr84K8mV71eqFwMx
    GS7e4iQP6guqW9biQfMA5/Id8ZjE7jNbF0LUGsY6Ktj+yOlAbTR+x5qr7Svb7oEs
    TMB/l9HBZ1WtIRzcUk9XYqzvYQr5TT997A63F28u32RchJ+5ECAz4g/p91aWxwVo
    HIfN10sGzttoukJCzC10CZAVscJB+nnoUbB/o3bPak6GUxBHpMgomb0K5g4Z4fXY
    4AZ9jKFoLgNcExdwteiUdSEnRorZ5Ny8sP84lwJziD3wuamVUsZ1C/KiQJBGTp5e
    LUY38J1oIwptw5fqjaAq2GQxEaIknWQ4fr3ZvNYUuGUt5FbHe5U5XF34gC8PK7v7
    bT/7sVdZZzKFScDLfH5N36M5FrXfTaXsVbfrRoa2j7U0kndyVEZyJsKVAQ8vgwbJ
    w/w2hKkyQLAg3l5yO5CHLGatsfSIzea4WoOAaroxiNtL9gzVXzqpw6qPEsH9hsws
    HsPEQWXHmDQvFTNUU14qic1Vc5fyxCBXIAGAPBd20b+219XznJ5uBKUgtvnqcItj
    nMYe6Btxh+pjrTA15X/p81z6sB7dkL1hPHfawLhCEzJbIPyyBTQYqY00/ap4Rj7t
    kzSiyzBejniFfAZ6eYBWsej7uXUsVndBF1ggZynPTeE=
    =iaEm
    -----END PGP PUBLIC KEY BLOCK-----
    ```

4) Import the OpenPGP key for Open Job Description by running the following command:

    ```
    gpg --import --armor openjobdescription-pgp.asc
    ```

5) Determine whether to trust the OpenPGP key. Some factors to consider when deciding whether or not to trust the above key are:

    - The internet connection you’ve used to obtain the GPG key from this website is secure
    - The device that you are accessing this website on is secure

    If you have decided to trust the OpenPGP key, then edit the key to trust with `gpg` like the following example:
    ```
    $ gpg --edit-key A2F0F059BCC40987
    gpg (GnuPG) 2.0.22; Copyright (C) 2013 Free Software Foundation, Inc.
    This is free software: you are free to change and redistribute it.
    There is NO WARRANTY, to the extent permitted by law.
    
    
    pub  4096R/BCC40987  created: 2024-02-09  expires: 2026-02-08  usage: SCEA
                         trust: unknown       validity: unknown
    [ unknown] (1). Open Job Description <openjobdescription@amazon.com>
    
    gpg> trust
    pub  4096R/BCC40987  created: 2024-02-09  expires: 2026-02-08  usage: SCEA
                         trust: unknown       validity: unknown
    [ unknown] (1). Open Job Description <openjobdescription@amazon.com>
    
    Please decide how far you trust this user to correctly verify other users' keys
    (by looking at passports, checking fingerprints from different sources, etc.)
    
      1 = I don't know or won't say
      2 = I do NOT trust
      3 = I trust marginally
      4 = I trust fully
      5 = I trust ultimately
      m = back to the main menu
    
    Your decision? 5
    Do you really want to set this key to ultimate trust? (y/N) y
    
    pub  4096R/BCC40987  created: 2024-02-09  expires: 2026-02-08  usage: SCEA
                         trust: ultimate      validity: unknown
    [ unknown] (1). Open Job Description <openjobdescription@amazon.com>
    Please note that the shown key validity is not necessarily correct
    unless you restart the program.
    
    gpg> quit
    ```

6) Verify the signature of the Open Job Description release via `gpg --verify`. The command for verifying the example files from step 1 would be:

    ```
    gpg --verify ./openjd_sessions-1.2.3-py3-none-any.whl.sig ./openjd_sessions-1.2.3-py3-none-any.whl
    ```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
