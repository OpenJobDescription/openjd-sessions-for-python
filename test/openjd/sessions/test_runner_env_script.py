# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
import time
from datetime import timedelta
from logging.handlers import QueueHandler
from pathlib import Path
from queue import SimpleQueue
from typing import Optional, Union
from unittest.mock import MagicMock, patch

import pytest

from openjd.model import SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import (
    CancelationMethodNotifyThenTerminate as CancelationMethodNotifyThenTerminate_2023_09,
)
from openjd.model.v2023_09 import (
    CancelationMethodTerminate as CancelationMethodTerminate_2023_09,
)
from openjd.model.v2023_09 import CancelationMode as CancelationMode_2023_09
from openjd.model.v2023_09 import (
    EmbeddedFileText as EmbeddedFileText_2023_09,
)
from openjd.model.v2023_09 import (
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)
from openjd.model.v2023_09 import (
    EnvironmentActions as EnvironmentActions_2023_09,
)
from openjd.model.v2023_09 import (
    EnvironmentScript as EnvironmentScript_2023_09,
)
from openjd.sessions import ActionState
from openjd.sessions._runner_base import ScriptRunnerState
from openjd.sessions._runner_env_script import (
    CancelMethod,
    EnvironmentScriptRunner,
    NotifyCancelMethod,
    TerminateCancelMethod,
)

from .conftest import build_logger, collect_queue_messages


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path", "message_queue", "queue_handler")
class TestEnvironmentScriptRunner:
    @pytest.mark.parametrize(
        "env_actions",
        [
            pytest.param(
                EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command="{{ Task.Command }}", args=["-c", "print('Hello')"]
                    )
                ),
                id="onEnter",
            ),
            pytest.param(
                EnvironmentActions_2023_09(
                    onExit=Action_2023_09(
                        command="{{ Task.Command }}", args=["-c", "print('Hello')"]
                    )
                ),
                id="onExit",
            ),
        ],
    )
    def test_run_basic(
        self,
        env_actions: EnvironmentActions_2023_09,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that run of an onEnter action with no embedded files works as expected.

        # GIVEN
        script = EnvironmentScript_2023_09(actions=env_actions)
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        if env_actions.onEnter is not None:
            runner.enter()
        else:
            runner.exit()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello" in messages

    @pytest.mark.parametrize(
        "env_actions",
        [
            pytest.param(
                EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command="{{ Task.Command }}", args=["-c", "print('Hello')"]
                    )
                ),
                id="onEnter",
            ),
            pytest.param(
                EnvironmentActions_2023_09(
                    onExit=Action_2023_09(
                        command="{{ Task.Command }}", args=["-c", "print('Hello')"]
                    )
                ),
                id="onExit",
            ),
        ],
    )
    def test_run_handles_none(
        self,
        env_actions: EnvironmentActions_2023_09,
        tmp_path: Path,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that when given an environment that doesn't have the corresponding
        # action defined we:
        # a) Don't explode;
        # b) Don't run anything; and
        # c) Invoke the callback

        # GIVEN
        script = EnvironmentScript_2023_09(actions=env_actions)
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        callback = MagicMock()
        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
            callback=callback,
        )

        # WHEN
        if env_actions.onExit is not None:
            runner.enter()
        else:
            runner.exit()

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        callback.assert_called_once_with(ActionState.SUCCESS)

    def test_run_handles_none_script(
        self,
        tmp_path: Path,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that when given an environment that doesn't have a script we:
        # a) Don't explode;
        # b) Don't run anything; and
        # c) Invoke the callback
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        callbackOnEnter = MagicMock()
        callbackOnExit = MagicMock()
        runnerOnEnter = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=None,
            symtab=symtab,
            session_files_directory=tmp_path,
            callback=callbackOnEnter,
        )
        runnerOnExit = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=None,
            symtab=symtab,
            session_files_directory=tmp_path,
            callback=callbackOnExit,
        )

        # WHEN
        runnerOnEnter.enter()
        runnerOnExit.exit()

        # THEN
        assert runnerOnEnter.state == ScriptRunnerState.SUCCESS
        callbackOnEnter.assert_called_once_with(ActionState.SUCCESS)
        assert runnerOnExit.state == ScriptRunnerState.SUCCESS
        callbackOnExit.assert_called_once_with(ActionState.SUCCESS)

    @pytest.mark.parametrize(
        "env_actions",
        [
            pytest.param(
                EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command="{{ Task.Command }}", args=["{{ Env.File.Foo }}"]
                    )
                ),
                id="onEnter",
            ),
            pytest.param(
                EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command="{{ Task.Command }}", args=["{{ Env.File.Foo }}"])
                ),
                id="onExit",
            ),
        ],
    )
    def test_run_with_files(
        self,
        env_actions: EnvironmentActions_2023_09,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that run of an action with embedded files works as expected.

        # GIVEN
        script = EnvironmentScript_2023_09(
            actions=env_actions,
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo", type=EmbeddedFileTypes_2023_09.TEXT, data="print('Hello')"
                )
            ],
        )
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        runner = EnvironmentScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        if env_actions.onEnter is not None:
            runner.enter()
        else:
            runner.exit()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello" in messages
        assert len(symtab.symbols) == 1

    @pytest.mark.parametrize(
        "env_actions",
        [
            pytest.param(
                EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command="{{ Task.Command }}", args=["{{ Env.File.Foo }}"]
                    )
                ),
                id="onEnter",
            ),
            pytest.param(
                EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command="{{ Task.Command }}", args=["{{ Env.File.Foo }}"])
                ),
                id="onExit",
            ),
        ],
    )
    def test_run_with_bad_files(
        self,
        env_actions: EnvironmentActions_2023_09,
        tmp_path: Path,
    ) -> None:
        # Test that run of an action with embedded files that cannot be materialized to
        # disk:
        # a) Doesn't blow up;
        # b) Sets the runner to the appropriate state; and
        # c) Invokes the callback

        # GIVEN
        script = EnvironmentScript_2023_09(
            actions=env_actions,
            embeddedFiles=[
                # Will fail materialization due to reference to non-existent value.
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="{{ Task.Not.A.Value }}",
                )
            ],
        )
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        callback = MagicMock()
        runner = EnvironmentScriptRunner(
            logger=MagicMock(),
            session_working_directory=tmp_path,
            environment_script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
            callback=callback,
        )

        # WHEN
        if env_actions.onEnter is not None:
            runner.enter()
        else:
            runner.exit()

        # THEN
        assert runner.state == ScriptRunnerState.FAILED
        callback.assert_called_once_with(ActionState.FAILED)

    @pytest.mark.parametrize(
        "cancel_method,expected",
        [
            pytest.param(None, TerminateCancelMethod(), id="default is terminate"),
            pytest.param(
                CancelationMethodTerminate_2023_09(mode=CancelationMode_2023_09.TERMINATE),
                TerminateCancelMethod(),
                id="terminate is terminate",
            ),
            pytest.param(
                CancelationMethodNotifyThenTerminate_2023_09(
                    mode=CancelationMode_2023_09.NOTIFY_THEN_TERMINATE
                ),
                NotifyCancelMethod(terminate_delay=timedelta(seconds=30)),
                id="default notify period is 30",
            ),
            pytest.param(
                CancelationMethodNotifyThenTerminate_2023_09(
                    mode=CancelationMode_2023_09.NOTIFY_THEN_TERMINATE, notifyPeriodInSeconds=10
                ),
                NotifyCancelMethod(terminate_delay=timedelta(seconds=10)),
                id="uses notify period",
            ),
        ],
    )
    def test_cancel(
        self,
        tmp_path: Path,
        cancel_method: Optional[
            Union[
                CancelationMethodNotifyThenTerminate_2023_09,
                CancelationMethodTerminate_2023_09,
            ]
        ],
        expected: CancelMethod,
    ) -> None:
        # Test that cancel invokes the base class' cancel with the appropriate arguments.

        # We'll do this one with mocks to avoid timing/race-condition issues.
        # The lower-level process runners have been thoroughly tested for cancel's
        # functionality, so this seems fine.

        with patch.object(EnvironmentScriptRunner, "_run_action"):
            with patch.object(EnvironmentScriptRunner, "_cancel") as mock_cancel:
                # GIVEN
                script = EnvironmentScript_2023_09(
                    actions=EnvironmentActions_2023_09(
                        onEnter=Action_2023_09(
                            command="{{ Task.Command }}",
                            args=["-c", "print('Hello')"],
                            cancelation=cancel_method,
                        )
                    )
                )

                symtab = SymbolTable(source={"Task.Command": sys.executable})
                runner = EnvironmentScriptRunner(
                    logger=MagicMock(),
                    session_working_directory=tmp_path,
                    environment_script=script,
                    symtab=symtab,
                    session_files_directory=tmp_path,
                )
                runner.enter()
                time_limit = timedelta(30)

                # WHEN
                runner.cancel(time_limit=time_limit)

                # THEN
                arg0 = mock_cancel.call_args.args[0]
                assert arg0 == expected
                arg1 = mock_cancel.call_args.args[1]
                assert arg1 is time_limit
