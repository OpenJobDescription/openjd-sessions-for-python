
# Development

## Command Reference

```
# Build the package
hatch build

# Run tests
hatch run test

# Run linting
hatch run lint

# Run formatting
hatch run fmt

# Run a full test
hatch run all:test
```

## Architecture

This module is responsible for providing functionality for a running Open Job Description Session.

The public interface is via the `Session` class. An instance of this class represents a single
running Session context, in the terms of the Open Job Description's Job Running Model.

The interface to a `Session` follows an asychronous computing model backed, internally,
by threads. The `Session` has a state that gates what is able to be performed, and when.
A user can begin running a new Action, whether that be the enter/exit of an Environment or 
the run-action of a Task, when the `Session` is in `READY` state. Running the action starts
background threads that will monitor the running subprocess, and forward its stdout/stderr to
a given Logger.

The internal mechanics of running an action in a `Session` looks like:

1. User calls `Session.enter_environment()`, `Session.exit_environment()`, or `Session.run_task()`
2. That creates a `StepScriptRunner` or `EnvironmentScriptRunner` (depending on the method called),
   and calls the `.enter()`, `.exit()` or `.run()` method as appropriate.
3. That, in turn:
    1. Uses a `EmbeddedFiles` instance to materialize any attachments/files from the script
       into a subdirectory of the Session's working directory.
    2. Creates a `LoggingSubprocess` and wires the callback of that instance to invoke a callback in the
       `*Runner` instance when the subprocess exits.
        1. The callback of the `*Runner` instance will, in turn, invoke a callback in the `Session` instance
           to tell the `Session` that the process has exited.
        2. Once called, the callback in the `Session` instance will call a callback that is provided to the
           `Session` when it is constructed, this asychronously informs the creator of the `Session`
           that the subprocess has exited.
    3. Runs the `LoggingSubprocess` within a Future and then returns while that runs.
        1. The thread/future that runs the `LoggingSubprocess`:
            1. Starts the subprocess
            2. Forwards all of the subprocess' stdout/stderr to the `logger` given to the `LoggingSubprocess`
            3. Invokes the callback that was given to the `LoggingSubprocess` when the subprocess exits

Canceling a running action is done via `Session.cancel()`. If there is a running action, that has not already been
canceled, then the `*Runner` instance that is running the action will start a Future thread that performs the
subprocess cancelation logic -- sending the appropriate signals at the appropriate times. Sending that signal
will cause the subprocess to exit, which will cause the `LoggingSubprocess` to invoke its callback signaling a
subprocess exit; and the chain of callbacks proceeding from there as per any other subprocess exit.

When a `Session` is created, we attach an `ActionMonitoringFilter` to the logger that was given
to the `Session`; this filter is removed from the logger when the `__del__()` method of the `Session`
is called -- so, users should `del session` when done with one. The `ActionMonitoringFilter` watches for
Open Job Description messages in the output stream from the running subprocess (these are lines that start with "openjd_"),
and invokes a callback in the `Session` when encountering one. This callback records info on the event
within the `Session`.

## Testing

This package strives for very high test coverage of its functionality. You are asked to help us maintain our
high bar by adding thorough test coverage for any changes you make and any testing gaps that you discover.

To run our tests simply run: `hatch run test`

If you have multiple version of Python installed (e.g. Python 3.9, 3.10, 3.11, etc) then you can run the tests
against all of your installed versions of python with: `hatch run all:test`

### User Impersonation

This library contains functionality to run subprocesses as a user other than the one that is
running the main process. You will need to take special steps to ensure that your changes
keep this functionality running in tip-top shape.

#### User Impersonation: POSIX-Based Systems

To run the impersonation tests you must create additional users and groups for the impersonation
tests on your local system and then set environment variables before running the tests.

Scripting has been added to this repository to test this functionality on Linux using
docker containers that we have set up for this purpose.

To run these tests:
1. With users configured locally in /etc/passwd & /etc/groups: `scripts/run_sudo_tests.sh --build`
2. With users via an LDAP client: `scripts/run_sudo_tests.sh --build --ldap`

If you are unable to use the provided docker container then you need to set up the `OPENJD_TEST_SUDO_*`
environment variables and their referenced users and groups as in the Dockerfile under
`testing_containers/localuser_sudo_environment/Dockerfile` in this repository.

#### User Impersonation: Windows-Based Systems

This library performs impersonation differently based on whether it is being run as part
of an OS Service (with Windows Session ID 0) or an interactive logon session (which has
Windows Session ID > 0). Thus, changes to the impersonation logic may need to be tested in
both of these environments.

To run the impersonation tests you will require a separate user on your workstation, and its
password, that you are able to logon as. Then:

1. Set the environment variable `OPENJD_TEST_WIN_USER_NAME` to the username of that user;
2. Set the environment variable `OPENJD_TEST_WIN_USER_PASSWORD` to that user's password; and
3. Then run the tests with `hatch run test` as normal.
    * If done correctly, then you should not see any xfail tests related to impersonation.

Run these tests in both:
1. A terminal in your interactive logon session to test the impersonation logic when 
   Windows Session ID > 0; and
2. An `ssh` terminal into your workstation to test the impersonation logic when Windows
   Session ID is 0.

## The Package's Public Interface

This package is a library wherein we are explicit and intentional with what we expose as public.

The standard convention in Python is to prefix things with an underscore character ('_') to
signify that the thing is private to the implementation, and is not intended to be used by
external consumers of the thing.

We use this convention in this package in two ways:

1. In filenames.
    1. Any file whose name is not prefixed with an underscore **is** a part of the public
    interface of this package. The name may not change and public symbols (classes, modules,
    functions, etc.) defined in the file may not be moved to other files or renamed without a
    major version number change.
    2. Any file whose name is prefixed with an underscore is an internal module of the package
    and is not part of the public interface. These files can be renamed, refactored, have symbols
    renamed, etc. Any symbol defined in one of these files that is intended to be part of this
    package's public interface must be imported into an appropriate `__init__.py` file.
2. Every symbol that is defined or imported in a public module and is not intended to be part
   of the module's public interface is prefixed with an underscore.

For example, a public module in this package will be defined with the following style:

```python
# The os module is not part of this file's external interface
import os as _os

# PublicClass is part of this file's external interface.
class PublicClass:
    def publicmethod(self):
        pass

    def _privatemethod(self):
        pass

# _PrivateClass is not part of this file's external interface.
class _PrivateClass:
    def publicmethod(self):
        pass

    def _privatemethod(self):
        pass
```

### On `import os as _os`

Every module/symbol that is imported into a Python module becomes a part of that module's interface.
Thus, if we have a module called `foo.py` such as:

```python
# foo.py

import os
```

Then, the `os` module becomes part of the public interface for `foo.py` and a consumer of that module
is free to do:

```python
from foo import os
```

We don't want all (generally, we don't want any) of our imports to become part of the public API for
the module, so we import modules/symbols into a public module with the following style:

```python
import os as _os
from typing import Dict as _Dict
```

## Use of Keyword-Only Arguments

Another convention that we are adopting in this package is that all functions/methods that are a
part of the package's external interface should refrain from using positional-or-keyword arguments.
All arguments should be keyword-only unless the argument name has no true external meaning (e.g.
arg1, arg2, etc for `min`). Benefits of this convention are:

1. All uses of the public APIs of this package are forced to be self-documenting; and
2. The benefits set forth in PEP 570 ( https://www.python.org/dev/peps/pep-0570/#problems-without-positional-only-parameters ).

## Exceptions

All functions/methods that raise an exception should have a section in their docstring that states
the exception(s) they raise. e.g.

```py
def my_function(key, value):
"""Does something...

    Raises:
        KeyError: when the key is not valid
        ValueError: when the value is not valid
"""
```

All function/method calls that can raise an exception should have a comment in the line above
that states which exception(s) can be raised. e.g.

```py
try:
    # Raises: KeyError, ValueError
    my_function("key", "value")
except ValueError as e:
    # Error handling...
```

## Super verbose test output

If you find that you need much more information from a failing test (say you're debugging a
deadlocking test) then a way to get verbose output from the test is to enable Pytest
[Live Logging](https://docs.pytest.org/en/latest/how-to/logging.html#live-logs):

1. Add a `pytest.ini` to the root directory of the repository that contains (Note: for some reason,
setting `log_cli` and `log_cli_level` in `pyproject.toml` does not work, nor does setting the options
on the command-line; if you figure out how to get it to work then please update this section):
```
[pytest]
xfail_strict = False
log_cli = true
log_cli_level = 10
```
2. Modify `pyproject.toml` to set the following additional `addopts` in the `tool.pytest.ini_options` section:
```
    "-vvvvv",
    "--numprocesses=1"
```
3. Add logging statements to your tests as desired and run the test(s) that you are debugging.
