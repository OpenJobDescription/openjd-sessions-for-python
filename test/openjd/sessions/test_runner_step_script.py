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
from openjd.model.v2023_09 import StepActions as StepActions_2023_09
from openjd.model.v2023_09 import StepScript as StepScript_2023_09

from openjd.sessions._runner_base import ScriptRunnerState
from openjd.sessions._runner_step_script import (
    CancelMethod,
    NotifyCancelMethod,
    StepScriptRunner,
    TerminateCancelMethod,
)

from .conftest import build_logger, collect_queue_messages


# tmp_path - builtin temporary directory
@pytest.mark.usefixtures("tmp_path", "message_queue", "queue_handler")
class TestStepScriptRunner:
    def test_run_basic(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that run of an action with no embedded files works as expected.

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command="{{ Task.Command }}", args=["-c", "print('\"Hello\"')"]
                )
            )
        )
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert '"Hello"' in messages

    def test_run_with_files(
        self,
        tmp_path: Path,
        message_queue: SimpleQueue,
        queue_handler: QueueHandler,
    ) -> None:
        # Test that run of an action with no embedded files works as expected.

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command="{{ Task.Command }}", args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo", type=EmbeddedFileTypes_2023_09.TEXT, data="print('Hello')"
                )
            ],
        )
        symtab = SymbolTable(source={"Task.Command": sys.executable})
        logger = build_logger(queue_handler)
        runner = StepScriptRunner(
            logger=logger,
            session_working_directory=tmp_path,
            script=script,
            symtab=symtab,
            session_files_directory=tmp_path,
        )

        # WHEN
        runner.run()
        while runner.state == ScriptRunnerState.RUNNING:
            time.sleep(0.2)

        # THEN
        assert runner.state == ScriptRunnerState.SUCCESS
        messages = collect_queue_messages(message_queue)
        assert "Hello" in messages
        assert len(symtab.symbols) == 1

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
                NotifyCancelMethod(terminate_delay=timedelta(seconds=120)),
                id="default notify period is 120s",
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

        with patch.object(StepScriptRunner, "_run_action"):
            with patch.object(StepScriptRunner, "_cancel") as mock_cancel:
                # GIVEN
                script = StepScript_2023_09(
                    actions=StepActions_2023_09(
                        onRun=Action_2023_09(
                            command="{{ Task.Command }}",
                            args=["-c", "print('Hello')"],
                            cancelation=cancel_method,
                        )
                    )
                )
                symtab = SymbolTable(source={"Task.Command": sys.executable})
                runner = StepScriptRunner(
                    logger=MagicMock(),
                    session_working_directory=tmp_path,
                    script=script,
                    symtab=symtab,
                    session_files_directory=tmp_path,
                )
                runner.run()
                time_limit = timedelta(30)

                # WHEN
                runner.cancel(time_limit=time_limit)

                # THEN
                arg0 = mock_cancel.call_args.args[0]
                assert arg0 == expected
                arg1 = mock_cancel.call_args.args[1]
                assert arg1 is time_limit
