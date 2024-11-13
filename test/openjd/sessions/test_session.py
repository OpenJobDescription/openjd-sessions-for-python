# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import json
import logging
import os
import stat
import sys
import time
import uuid
from datetime import timedelta
from pathlib import PurePosixPath, PureWindowsPath, Path
from typing import Any, Optional
from unittest.mock import ANY, MagicMock, PropertyMock, patch, call
from subprocess import DEVNULL, run

import pytest

from openjd.model import ParameterValue, ParameterValueType, SpecificationRevision, SymbolTable
from openjd.model.v2023_09 import Action as Action_2023_09
from openjd.model.v2023_09 import (
    EmbeddedFileText as EmbeddedFileText_2023_09,
)
from openjd.model.v2023_09 import (
    EmbeddedFileTypes as EmbeddedFileTypes_2023_09,
)
from openjd.model.v2023_09 import Environment as Environment_2023_09
from openjd.model.v2023_09 import (
    EnvironmentActions as EnvironmentActions_2023_09,
)
from openjd.model.v2023_09 import (
    EnvironmentScript as EnvironmentScript_2023_09,
)
from openjd.model.v2023_09 import StepActions as StepActions_2023_09
from openjd.model.v2023_09 import StepScript as StepScript_2023_09
from openjd.sessions import (
    LOG,
    ActionState,
    ActionStatus,
    PathFormat,
    PathMappingRule,
    Session,
    SessionState,
    LogContent,
)
from openjd.sessions import _path_mapping as path_mapping_impl_mod
from openjd.sessions._action_filter import ActionMessageKind
from openjd.sessions._os_checker import is_posix, is_windows
from openjd.sessions._logging import LoggerAdapter, LogExtraInfo
from openjd.sessions._session import (
    EnvironmentVariableChange,
    EnvironmentVariableSetChange,
    EnvironmentVariableUnsetChange,
    SimplifiedEnvironmentVariableChanges,
)
from openjd.sessions._session_user import PosixSessionUser, WindowsSessionUser
from openjd.sessions._windows_permission_helper import WindowsPermissionHelper

from .conftest import (
    has_posix_target_user,
    has_windows_user,
    WIN_SET_TEST_ENV_VARS_MESSAGE,
    POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
)


def _environment_from_script(script: EnvironmentScript_2023_09) -> Environment_2023_09:
    return Environment_2023_09(name="DefinitelyNotAFakeEnvironment", script=script)


class TestSessionLogging:
    def test_log_record_fields(self, caplog) -> None:
        "Confirms that both the session_id and log metadata fields are available in the session log"
        # GIVEN
        session_id = uuid.uuid4().hex
        session = Session(session_id=session_id, job_parameter_values={})

        # WHEN
        session._logger.info(
            "Process 9001 failed to stop.",
            extra=LogExtraInfo(openjd_log_content=LogContent.PROCESS_CONTROL),
        )

        # THEN
        record = next(
            (
                record
                for record in caplog.records
                if record.message == "Process 9001 failed to stop."
            ),
            None,
        )
        assert record

        assert hasattr(record, "session_id")
        assert record.session_id == session_id

        assert hasattr(record, "openjd_log_content")
        assert isinstance(record.openjd_log_content, LogContent)
        assert record.openjd_log_content == LogContent.PROCESS_CONTROL


class TestSessionInitialization:
    def test_initiaize_basic(self) -> None:
        # Test of the basic functionality of a Session's initialization.
        # Should create the working directories and set up a log handler on LOG,
        # and a log filter on that handler.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}

        # WHEN
        session = Session(session_id=session_id, job_parameter_values=job_params)

        # THEN
        assert session.state == SessionState.READY
        assert os.path.exists(session.working_directory)
        assert os.path.exists(session.files_directory)
        assert session._session_id == session_id
        assert session._job_parameter_values == job_params
        assert session._job_parameter_values is not job_params
        assert isinstance(session._logger, LoggerAdapter)
        assert "session_id" in session._logger.extra
        assert session._logger.extra["session_id"] == session_id
        assert session._log_filter in LOG.filters

        session.cleanup()

    @pytest.mark.usefixtures("tmp_path")  # built-in fixture
    def test_initialize_with_root_dir(self, tmp_path: Path) -> None:
        # Test that we create the Session Working Directory in the given directory, if there is one.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        # WHEN
        session = Session(
            session_id=session_id, job_parameter_values=job_params, session_root_directory=tmp_path
        )

        # THEN
        assert session.working_directory.parent == tmp_path

    @pytest.mark.usefixtures("tmp_path")  # built-in fixture
    def test_initialize_raises_with_bad_root_dir(self, tmp_path: Path) -> None:
        # Test that we create the Session Working Directory in the given directory, if there is one.

        # GIVEN
        root_dir = tmp_path / "does_not_exist"
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()

        # THEN
        with pytest.raises(RuntimeError):
            Session(
                session_id=session_id,
                job_parameter_values=job_params,
                session_root_directory=root_dir,
            )

    def test_root_dir_permissions(self) -> None:
        # Ensures that the permissions of the session root dir
        # allow:
        #  The owner r/w/x
        #  The group nothing (it doesn't need it since we create subdirs in it for each Session)
        #  The world r/x so that subdirs within it can be accessed by the Session's user.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        session = Session(session_id=session_id, job_parameter_values=job_params)

        # WHEN
        filename = session._openjd_session_root_dir()

        # THEN
        if is_posix():
            statinfo = os.stat(filename)
            assert statinfo.st_mode & stat.S_IRWXU == stat.S_IRWXU, "owner r/w/x"
            assert statinfo.st_mode & stat.S_IRWXG == (stat.S_IRGRP | stat.S_IXGRP), "group r/x"
            assert statinfo.st_mode & stat.S_IRWXO == (stat.S_IROTH | stat.S_IXOTH), "other r/x"
        else:
            # TODO: Exact ACL checks in Python on Windows are more complex.
            #  Need to check this again during Windows impersonation
            # Basic permission checks on Windows for the current user
            assert os.access(
                filename, os.R_OK
            ), "The directory should be readable by the current user"
            assert os.access(
                filename, os.W_OK
            ), "The directory should be writable by the current user"
            assert os.access(
                filename, os.X_OK
            ), "The directory should be executable by the current user"

    def test_cleanup(self) -> None:
        # Test of the functionality of a Session's cleanup.
        # This should nuke the working directory and disconnect its handler from LOG

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}
        session = Session(session_id=session_id, job_parameter_values=job_params)
        working_dir = session.working_directory
        filter = session._log_filter

        # WHEN
        session.cleanup()

        # THEN
        assert not os.path.exists(working_dir)
        assert filter not in LOG.filters
        assert session.state == SessionState.ENDED

    @pytest.mark.skipif(os.name != "posix", reason="Posix-only test.")
    @pytest.mark.xfail(
        not has_posix_target_user(),
        reason=POSIX_SET_TARGET_USER_ENV_VARS_MESSAGE,
    )
    @pytest.mark.usefixtures("posix_target_user")
    @pytest.mark.usefixtures("caplog")  # built-in fixture
    def test_cleanup_posix_user(
        self, posix_target_user: PosixSessionUser, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Test of the functionality of a Session's cleanup when files may have been
        # written to the directory by a separate user
        # This should nuke the working directory and disconnect its handler from LOG

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}
        session = Session(
            session_id=session_id, job_parameter_values=job_params, user=posix_target_user
        )
        working_dir = session.working_directory
        # Create a directory and file that are owned by the target user & its self-named group,
        # with owner-only permissions.
        # We cannot delete these as our current user, so this only passes if we're correctly
        # deleting the working directory as the target user.
        runresult = run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "mkdir",
                str(working_dir / "subdir"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chown",
                # Make sure that this process' user cannot delete subdir by changing its group.
                # We're relying on the user running the test not being in the 'user' group of the target user
                # This is how our testing Docker container is set up.
                f"{posix_target_user.user}:{posix_target_user.user}",
                str(working_dir / "subdir"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chmod",
                "700",  # Only owner has permissions
                str(working_dir / "subdir"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "touch",
                str(working_dir / "subdir" / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chown",
                # We're relying on the user running the test not being in the 'user' group of the target user
                # This is how our testing Docker container is set up.
                f"{posix_target_user.user}:{posix_target_user.user}",
                str(working_dir / "subdir" / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chmod",
                "600",
                str(working_dir / "subdir" / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "touch",
                str(working_dir / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chown",
                # We're relying on the user running the test not being in the 'user' group of the target user
                # This is how our testing Docker container is set up.
                f"{posix_target_user.user}:{posix_target_user.user}",
                str(working_dir / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode
        runresult |= run(
            [
                "sudo",
                "-u",
                posix_target_user.user,
                "-i",
                "chmod",
                "600",
                str(working_dir / "file.test"),
            ],
            stdin=DEVNULL,
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).returncode

        # WHEN
        session.cleanup()

        # THEN
        assert runresult == 0
        assert not os.path.exists(working_dir)
        assert all("rm: cannot remove" not in msg for msg in caplog.messages)

    @pytest.mark.skipif(not is_windows(), reason="Windows-only test.")
    @pytest.mark.xfail(not has_windows_user(), reason=WIN_SET_TEST_ENV_VARS_MESSAGE)
    @pytest.mark.timeout(90)
    def test_cleanup_windows_user(
        self,
        windows_user: WindowsSessionUser,
    ) -> None:
        # Test of the functionality of a Session's cleanup when files may have been
        # written to the directory by a separate user
        # This should nuke the working directory and disconnect its handler from LOG

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}
        session = Session(session_id=session_id, job_parameter_values=job_params, user=windows_user)
        working_dir = session.working_directory

        # Create a directory and file that are owned by the Windows test user,
        working_dir_file_path = str(working_dir / "file.test")
        subdir_path = str(working_dir / "subdir")
        subdir_file_path = str(working_dir / "subdir" / "file.test")

        os.mkdir(subdir_path)
        with open(subdir_file_path, "w") as f:
            f.write("File content")
        with open(working_dir_file_path, "w") as f:
            f.write("File content")

        WindowsPermissionHelper.set_permissions(
            subdir_path, principals_full_control=[windows_user.user]
        )
        WindowsPermissionHelper.set_permissions(
            subdir_file_path, principals_full_control=[windows_user.user]
        )
        WindowsPermissionHelper.set_permissions(
            working_dir_file_path, principals_full_control=[windows_user.user]
        )

        session.cleanup()

        # THEN
        assert not os.path.exists(working_dir)

    def test_contextmanager(self, session_id: str) -> None:
        # Test the context manager interface of the Session

        # GIVEN
        job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}

        # WHEN
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            working_dir = session.working_directory
            filter = session._log_filter

        # THEN - check that the cleanup has been done.
        assert not os.path.exists(working_dir)
        assert filter not in LOG.filters

    @pytest.mark.parametrize("method", ["_create_working_directory", "_create_files_directory"])
    @pytest.mark.usefixtures("caplog")  # built-in fixture
    def test_failed_directory_create(self, method: str, caplog: pytest.LogCaptureFixture) -> None:
        # Test that we immediately end the Session and send an error to the log if
        # creating the directory raises an error.
        with patch.object(Session, method) as method_mock:
            # GIVEN
            method_mock.side_effect = RuntimeError("Permission denied")
            session_id = uuid.uuid4().hex
            job_params = {"foo": ParameterValue(type=ParameterValueType.STRING, value="bar")}
            expected_error = "ERROR creating Session Working Directory: Permission denied"

            # WHEN
            with pytest.raises(RuntimeError):
                with Session(session_id=session_id, job_parameter_values=job_params):
                    pass

            # THEN
            assert any(msg == expected_error for msg in caplog.messages)
            error_record = tuple(
                r for r in filter(lambda rec: rec.msg == expected_error, caplog.records)
            )[0]
            assert error_record.levelno == logging.ERROR

    @pytest.mark.usefixtures("caplog")  # built-in fixture
    def test_posix_permissions_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # On POSIX systems, we check the sticky bit of the system /tmp dir
        # If it is not set, then we emit a security warning into the logs.
        # This tests that we do in fact emit that message when the sticky bit isn't set.
        with patch("openjd.sessions._session.TempDir", MagicMock()):
            with patch("openjd.sessions._session.os_name", "posix"):
                with patch("openjd.sessions._session.os_stat", MagicMock()) as os_stat_mock:
                    # GIVEN
                    # Sticky bit is not set
                    class StatReturn:
                        st_mode = 0xFFFFFFFF ^ stat.S_ISVTX

                    os_stat_mock.return_value = StatReturn()
                    session_id = uuid.uuid4().hex
                    job_params = {
                        "foo": ParameterValue(type=ParameterValueType.STRING, value="bar")
                    }
                    expected_err_prefix = "Sticky bit is not set"

                    # WHEN
                    with Session(session_id=session_id, job_parameter_values=job_params) as session:
                        # THEN
                        assert session.state == SessionState.READY
                        assert any(msg.startswith(expected_err_prefix) for msg in caplog.messages)
                        error_record = tuple(
                            r
                            for r in filter(
                                lambda rec: rec.msg.startswith(expected_err_prefix), caplog.records
                            )
                        )[0]
                        assert error_record.levelno == logging.WARNING

    @pytest.mark.usefixtures("caplog")  # built-in fixture
    def test_posix_permissions_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # This is the inverse test of test_posix_permissions_warning()
        with patch("openjd.sessions._session.TempDir", MagicMock()):
            with patch("openjd.sessions._session.os_name", "posix"):
                with patch("openjd.sessions._session.os_stat", MagicMock()) as os_stat_mock:
                    # GIVEN
                    # Sticky bit is not set
                    class StatReturn:
                        st_mode = stat.S_ISVTX

                    os_stat_mock.return_value = StatReturn()
                    session_id = uuid.uuid4().hex
                    job_params = {
                        "foo": ParameterValue(type=ParameterValueType.STRING, value="bar")
                    }
                    expected_err_prefix = "WARNING: Sticky bit is not set"

                    # WHEN
                    with Session(session_id=session_id, job_parameter_values=job_params) as session:
                        # THEN
                        assert session.state == SessionState.READY
                        assert not any(
                            msg.startswith(expected_err_prefix) for msg in caplog.messages
                        )


class TestSessionCallbacks:
    """Making sure that the session methods that are callbacks also call the user-provided
    callback with the expected data.
    """

    @pytest.mark.parametrize(
        "kind,value,expected",
        [
            pytest.param(
                ActionMessageKind.PROGRESS,
                50.0,
                ActionStatus(state=ActionState.RUNNING, progress=50.0),
                id="progress message",
            ),
            pytest.param(
                ActionMessageKind.STATUS,
                "foo",
                ActionStatus(state=ActionState.RUNNING, status_message="foo"),
                id="status message",
            ),
            pytest.param(
                ActionMessageKind.FAIL,
                "fail",
                ActionStatus(state=ActionState.RUNNING, fail_message="fail"),
                id="fail message",
            ),
        ],
    )
    def test_log_filter_callback(
        self, kind: ActionMessageKind, value: Any, expected: ActionStatus
    ) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()
        with Session(
            session_id=session_id, job_parameter_values=job_params, callback=callback
        ) as session:
            session._action_state = ActionState.RUNNING

            # WHEN
            session._action_log_filter_callback(kind, value)

            # THEN
            callback.assert_called_once_with(session_id, expected)

    @pytest.mark.parametrize(
        "kind,value",
        [
            pytest.param(
                ActionMessageKind.ENV,
                {"name": "foo", "value": "bar"},
                id="env message",
            ),
        ],
    )
    def test_log_filter_env(self, kind: ActionMessageKind, value: Any) -> None:
        # Ensure that message kinds that shouldn't be invoking the session's
        # callback aren't.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()
        with Session(
            session_id=session_id, job_parameter_values=job_params, callback=callback
        ) as session:
            session._action_state = ActionState.RUNNING
            identifier = "some-env-id"
            changes = SimplifiedEnvironmentVariableChanges(dict[str, str]())
            session._created_env_vars[identifier] = changes
            session._running_environment_identifier = identifier

            # WHEN
            session._action_log_filter_callback(kind, value)

            # THEN
            callback.assert_not_called()
            assert changes._to_set.get("FOO" if is_windows() else "foo") == "bar"

    @pytest.mark.parametrize(
        "state,exit_code,expected",
        [
            pytest.param(
                ActionState.SUCCESS,
                0,
                ActionStatus(state=ActionState.SUCCESS, exit_code=0),
                id="success exit",
            ),
            pytest.param(
                ActionState.FAILED,
                1,
                ActionStatus(state=ActionState.FAILED, exit_code=1),
                id="fail with exit code",
            ),
            # This happens when we have a failure before running the subprocess
            pytest.param(
                ActionState.FAILED,
                None,
                ActionStatus(state=ActionState.FAILED),
                id="fail no exit code",
            ),
            pytest.param(
                ActionState.CANCELED,
                -15,
                ActionStatus(state=ActionState.CANCELED, exit_code=-15),
                id="canceled by kill",
            ),
        ],
    )
    def test_action_callback(
        self, state: ActionState, exit_code: Optional[int], expected: ActionStatus
    ) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()
        with Session(
            session_id=session_id, job_parameter_values=job_params, callback=callback
        ) as session:
            session._action_state = ActionState.RUNNING
            mock_runner = MagicMock()
            type(mock_runner).exit_code = PropertyMock(return_value=exit_code)
            session._runner = mock_runner

            # WHEN
            session._action_callback(state)

            # THEN
            callback.assert_called_once_with(session_id, expected)


class TestSessionRunTask_2023_09:  # noqa: N801
    """Testing running tasks with the 2023-09 schema."""

    @staticmethod
    @pytest.fixture
    def fix_basic_task_script() -> StepScript_2023_09:
        return StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import time; time.sleep(0.5); print('{{ Task.Param.P }} {{ Task.RawParam.P }}'); print('{{ Param.J }} {{ RawParam.J }}')",
                )
            ],
        )

    @staticmethod
    @pytest.fixture
    def fix_foo_baz_environment() -> Environment_2023_09:
        return Environment_2023_09(name="FooBazEnvironment", variables={"FOO": "bar", "BAZ": "qux"})

    def test_run_task(
        self, caplog: pytest.LogCaptureFixture, fix_basic_task_script: StepScript_2023_09
    ) -> None:
        # GIVEN
        # Crafting a StepScript that ensures that references both Job & Task parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        task_params = {"P": ParameterValue(type=ParameterValueType.STRING, value="Pvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_task(step_script=fix_basic_task_script, task_parameter_values=task_params)

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "Jvalue Jvalue" in caplog.messages
            assert "Pvalue Pvalue" in caplog.messages

    def test_run_task_with_env_vars(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN
        step_script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data='import time; import os; time.sleep(0.5); print(f\'{os.environ["SESSION_VAR"]} {os.environ["ACTION_VAR"]}\')',
                )
            ],
        )

        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        session_env_vars = {"SESSION_VAR": "session_value"}
        action_env_vars = {"ACTION_VAR": "action_value"}
        with Session(
            session_id=session_id, job_parameter_values=job_params, os_env_vars=session_env_vars
        ) as session:
            # WHEN
            session.run_task(
                step_script=step_script,
                task_parameter_values=task_params,
                os_env_vars=action_env_vars,
            )

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "session_value action_value" in caplog.messages

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(state, id=state.value)
            for state in SessionState
            if state != SessionState.READY
        ],
    )
    def test_cannot_run_not_ready(self, state: SessionState) -> None:
        # This is checking that we cannot run a task unless the Session is READY

        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session._state = state

            # THEN
            with pytest.raises(RuntimeError):
                session.run_task(step_script=script, task_parameter_values=task_params)

    def test_run_task_fail_early(self) -> None:
        # Testing a task that fails before running.
        # This'll fail because we're referencing a Task parameter that doesn't exist.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        task_params = dict[str, ParameterValue]()
        step_script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import time; time.sleep(0.5); print('{{ Task.Param.P }}'); print('{{ Param.J }}')",
                )
            ],
        )
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_task(step_script=step_script, task_parameter_values=task_params)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(
                state=ActionState.FAILED,
                fail_message="Error resolving format string: Failed to parse interpolation expression at [37, 55]. Expression:  Task.Param.P . Reason: Expression failed validation: Task.Param.P has no value.",
            )

    def test_run_task_fail_run(self) -> None:
        # Testing a task that fails while running

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import sys; sys.exit(1)",
                )
            ],
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.run_task(step_script=script, task_parameter_values=task_params)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.FAILED, exit_code=1)

    def test_no_task_run_after_fail(self, fix_basic_task_script: StepScript_2023_09) -> None:
        # Testing that we cannot run a task if we've had a failure.
        # This'll fail because we're referencing a Task parameter that doesn't exist.

        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        task_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session._state = SessionState.READY_ENDING

            # THEN
            with pytest.raises(RuntimeError):
                session.run_task(
                    step_script=fix_basic_task_script, task_parameter_values=task_params
                )

    def test_run_task_with_variables(
        self,
        fix_basic_task_script: StepScript_2023_09,
        fix_foo_baz_environment: Environment_2023_09,
    ) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        task_params = {"P": ParameterValue(type=ParameterValueType.STRING, value="Pvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=fix_foo_baz_environment)
            assert session.state == SessionState.READY
            session.run_task(step_script=fix_basic_task_script, task_parameter_values=task_params)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            # THEN
            assert session._runner is not None
            assert fix_foo_baz_environment.variables is not None
            assert session._runner._os_env_vars == dict(fix_foo_baz_environment.variables)


class TestSessionCancel:
    """Test that cancelation will cancel the currently running Script."""

    def test_cancel(self) -> None:
        # Testing a task that fails while running

        # GIVEN
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import time; print('Starting'); time.sleep(10); print('End')",
                )
            ],
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.run_task(step_script=script, task_parameter_values=task_params)
            time.sleep(0.5)  # A bit of time to startup

            # WHEN
            session.cancel_action()
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            if is_posix():
                # Note: Posix - We cancel via SIGKILL. The process will exit with negative the SIGKILL's value (i.e. -9)
                assert session.action_status == ActionStatus(
                    state=ActionState.CANCELED, exit_code=-9
                )
            else:
                # Note: Windows - We cancel via psutil. The process will exist with 15.
                assert session.action_status == ActionStatus(
                    state=ActionState.CANCELED, exit_code=15
                )

    @pytest.mark.parametrize(
        argnames="time_limit",
        argvalues=(
            (
                None,
                timedelta(seconds=1),
                timedelta(seconds=2),
            )
            if is_posix()
            else (
                None,
                timedelta(seconds=2),
                timedelta(seconds=3),
            )
        ),
    )
    def test_cancel_time_limit(self, time_limit: Optional[timedelta]) -> None:
        # Testing that the time_limit argument is forwarded to the runner

        # GIVEN
        start_time = time.monotonic()
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Foo }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import time; time.sleep(10)",
                )
            ],
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.run_task(step_script=script, task_parameter_values=task_params)
            time.sleep(0.5)  # A bit of time to startup

            with patch.object(
                session._runner,
                "cancel",
                wraps=session._runner.cancel,  # type: ignore[union-attr]
            ) as runner_cancel_spy:
                # WHEN
                session.cancel_action(time_limit=time_limit)

            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            end_time = time.monotonic()

        # THEN
        duration_seconds = end_time - start_time
        runner_cancel_spy.assert_called_once_with(time_limit=time_limit, mark_action_failed=False)
        assert session.state == SessionState.ENDED
        if time_limit is not None:
            # Add some padding for the sleeps
            padding_time = timedelta(seconds=0.8) if is_posix() else timedelta(seconds=3)
            assert duration_seconds <= (time_limit + padding_time).total_seconds()


def _make_environment(
    enter_script: bool = False,
    exit_script: bool = False,
    variables: Optional[dict[str, str]] = None,
    name: Optional[str] = None,
) -> Environment_2023_09:
    script = (
        EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=(
                    Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                    if enter_script
                    else None
                ),
                onExit=(
                    Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                    if exit_script
                    else None
                ),
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Foo",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import time; time.sleep(0.5); print('{{ Param.J }}')",
                )
            ],
        )
        if enter_script or exit_script
        else None
    )

    return Environment_2023_09(
        name=name if name is not None else "SomeEnv", variables=variables, script=script
    )


class TestSessionEnterEnvironment_2023_09:  # noqa: N801
    """Testing running tasks with the 2023-09 schema."""

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_enter_environment_basic(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import time; time.sleep(0.5); print('{{ Param.J }} {{ RawParam.J }}')",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier = session.enter_environment(environment=environment)

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert len(session.environments_entered) == 1
            assert session.environments_entered[0] == identifier
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "Jvalue Jvalue" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_enter_environment_with_env_vars(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data='import time; import os; time.sleep(0.5); print(f\'{os.environ["SESSION_VAR"]} {os.environ["ACTION_VAR"]}\')',
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        session_env_vars = {"SESSION_VAR": "session_value"}
        action_env_vars = {"ACTION_VAR": "action_value"}
        with Session(
            session_id=session_id, job_parameter_values=job_params, os_env_vars=session_env_vars
        ) as session:
            # WHEN
            identifier = session.enter_environment(
                environment=environment, os_env_vars=action_env_vars
            )

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert len(session.environments_entered) == 1
            assert session.environments_entered[0] == identifier
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "session_value action_value" in caplog.messages

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(state, id=state.value)
            for state in SessionState
            if state != SessionState.READY
        ],
    )
    def test_cannot_enter_when_not_ready(self, state: SessionState) -> None:
        # This is checking that we cannot enter an environment unless the Session
        # is in READY state

        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session._state = state

            # THEN
            with pytest.raises(RuntimeError):
                session.enter_environment(environment=environment)

    def test_enter_two_environments(self) -> None:
        # This is checking that we construct the list of entered environments
        # correctly (i.e. the order of identifiers in the list is the order in which
        # they were entered)

        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        script = EnvironmentScript_2023_09(
            actions=EnvironmentActions_2023_09(
                onEnter=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
            ),
        )
        environment1 = _environment_from_script(script)
        environment2 = _environment_from_script(script)
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier1 = session.enter_environment(environment=environment1)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            identifier2 = session.enter_environment(environment=environment2)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.environments_entered == (identifier1, identifier2)

    def test_cannot_enter_same_environment_twice(self) -> None:
        # This is checking that we cannot enter the same environment twice, as defined by its
        # identifier

        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier1 = session.enter_environment(environment=environment)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            with pytest.raises(RuntimeError):
                session.enter_environment(environment=environment, identifier=identifier1)

    def test_enter_environment_fail_early(self) -> None:
        # Testing an environment that fails before running.
        # This'll fail because we're referencing a Task parameter that doesn't exist.

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import time; time.sleep(0.5); print('{{ Task.Param.P }}'); print('{{ Param.J }}')",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=environment)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(
                state=ActionState.FAILED,
                fail_message="Error resolving format string: Failed to parse interpolation expression at [37, 55]. Expression:  Task.Param.P . Reason: Expression failed validation: Task.Param.P has no value.",
            )

    def test_enter_environment_fail_run(self) -> None:
        # Testing an Environment enter that fails while running

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import sys; sys.exit(1)",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=environment)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.FAILED, exit_code=1)

    def test_enter_no_action(self, caplog: pytest.LogCaptureFixture) -> None:
        # Testing an environment enter where the given environment has no
        # onEnter action defined.

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=environment)

            # THEN
            assert session.state == SessionState.READY
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS)

    def test_enter_environment_with_variables(self) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        variables = {
            "FOO": "bar",
        }
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(
                environment=_make_environment(enter_script=True, variables=variables)
            )
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert session.state == SessionState.READY
            assert session._runner is not None
            assert session._runner._os_env_vars == dict(variables)

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_enter_environment_with_resolved_variables(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        variables = {
            "FOO": "{{Param.J}}",
        }
        environment = Environment_2023_09(
            name="DefinitelyNotAFakeEnvironment",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import time; import os; time.sleep(0.5); print(os.environ['FOO'])",
                    )
                ],
            ),
            variables=variables,
        )
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=environment)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert session.state == SessionState.READY
            assert session._runner is not None
            assert "Jvalue" in caplog.messages

    def test_enter_two_environments_with_variables(self) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        variables1 = {
            "FOO": "bar",
        }
        variables2 = {
            "FOO": "corge",
        }
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            session.enter_environment(environment=_make_environment(variables=variables1))
            assert session.state == SessionState.READY

            session.enter_environment(
                environment=_make_environment(enter_script=True, variables=variables2)
            )
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert session._runner is not None
            assert session._runner._os_env_vars == dict(variables2)


class TestSessionExitEnvironment_2023_09:  # noqa: N801
    """Testing running tasks with the 2023-09 schema."""

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_exit_environment_basic(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import time; time.sleep(0.5); print('{{ Param.J }} {{ RawParam.J }}')",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session.exit_environment(identifier=identifier)

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert len(session.environments_entered) == 0
            assert identifier not in session._environments
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "Jvalue Jvalue" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_exit_environment_with_env_vars(self, caplog: pytest.LogCaptureFixture) -> None:
        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data='import time; import os; time.sleep(0.5); print(f\'{os.environ["SESSION_VAR"]} {os.environ["ACTION_VAR"]}\')',
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        session_env_vars = {"SESSION_VAR": "session_value"}
        action_env_vars = {"ACTION_VAR": "action_value"}
        with Session(
            session_id=session_id, job_parameter_values=job_params, os_env_vars=session_env_vars
        ) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session.exit_environment(identifier=identifier, os_env_vars=action_env_vars)

            # THEN
            assert session.state == SessionState.RUNNING
            assert session.action_status == ActionStatus(state=ActionState.RUNNING)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            assert len(session.environments_entered) == 0
            assert identifier not in session._environments
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS, exit_code=0)
            assert "session_value action_value" in caplog.messages

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param(state, id=state.value)
            for state in SessionState
            if state not in (SessionState.READY, SessionState.READY_ENDING)
        ],
    )
    def test_cannot_exit_when_not_ready(self, state: SessionState) -> None:
        # This is checking that we cannot exit an environment unless the Session
        # is in READY or READY_ENDING state

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session._state = state

            # THEN
            with pytest.raises(RuntimeError):
                session.exit_environment(identifier=identifier)

    def test_exit_with_two_environments(self) -> None:
        # This is checking that we can only exit the most recently entered environment

        # GIVEN
        # Crafting a EnvironmentScript that ensures that references to Job parameters.
        # This ensures that we are correctly constructing the symbol table for the run.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier1 = session.enter_environment(environment=environment)
            session.enter_environment(environment=environment)

            # THEN
            with pytest.raises(RuntimeError):
                session.exit_environment(identifier=identifier1)

    def test_exit_environment_fail_early(self) -> None:
        # Testing an environment that fails before running.
        # This'll fail because we're referencing a Task parameter that doesn't exist.

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import time; time.sleep(0.5); print('{{ Task.Param.P }}'); print('{{ Param.J }}')",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session.exit_environment(identifier=identifier)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(
                state=ActionState.FAILED,
                fail_message="Error resolving format string: Failed to parse interpolation expression at [37, 55]. Expression:  Task.Param.P . Reason: Expression failed validation: Task.Param.P has no value.",
            )

    def test_exit_environment_fail_run(self) -> None:
        # Testing an Environment enter that fails while running

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Foo }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Foo",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import sys; sys.exit(1)",
                    )
                ],
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session.exit_environment(identifier=identifier)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.FAILED, exit_code=1)

    def test_exit_no_action(self, caplog: pytest.LogCaptureFixture) -> None:
        # Testing an environment exit where the given environment has no
        # onExit action defined.

        # GIVEN
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["-c", "print('hi')"])
                ),
            )
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            identifier = session.enter_environment(environment=environment)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.exit_environment(identifier=identifier)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session.action_status == ActionStatus(state=ActionState.SUCCESS)

    def test_exit_environment_with_variables(self) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        variables = {
            "FOO": "bar",
        }
        environment = _make_environment(enter_script=False, exit_script=True, variables=variables)
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier = session.enter_environment(environment=environment)
            assert session.state == SessionState.READY

            session.exit_environment(identifier=identifier)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session._runner is not None
            assert session._runner._os_env_vars == dict(variables)

    def test_exit_two_environments_with_variables(self) -> None:
        # GIVEN
        session_id = uuid.uuid4().hex
        job_params = {"J": ParameterValue(type=ParameterValueType.STRING, value="Jvalue")}
        variables1 = {
            "FOO": "bar",
        }
        variables2 = {
            "FOO": "corge",
            "BAZ": "QUX",
        }
        environment1 = _make_environment(enter_script=False, exit_script=True, variables=variables1)
        environment2 = _make_environment(enter_script=False, exit_script=True, variables=variables2)
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            # WHEN
            identifier1 = session.enter_environment(environment=environment1)
            assert session.state == SessionState.READY

            identifier2 = session.enter_environment(environment=environment2)
            assert session.state == SessionState.READY

            session.exit_environment(identifier=identifier2)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session._runner is not None
            assert session._runner._os_env_vars == dict(variables2)

            session.exit_environment(identifier=identifier1)
            # Wait for the process to exit
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert session._runner is not None
            assert session._runner._os_env_vars == dict(variables1)


class TestPathMapping_v2023_09:  # noqa: N801
    """Tests that path mapping works with the 2023-09 schema."""

    @pytest.mark.parametrize(
        "rules,expected_json",
        [
            pytest.param(None, json.dumps({}), id="None path mapping"),
            pytest.param([], json.dumps({}), id="Empty path mapping"),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/home/user"),
                        destination_path=PurePosixPath("/mnt/share/user"),
                    ),
                ],
                json.dumps(
                    {
                        "version": "pathmapping-1.0",
                        "path_mapping_rules": [
                            {
                                "source_path_format": "POSIX",
                                "source_path": "/home/user",
                                "destination_path": "/mnt/share/user",
                            }
                        ],
                    }
                ),
                id="single posix",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.WINDOWS,
                        source_path=PureWindowsPath(r"c:\Users\user"),
                        destination_path=PurePosixPath("/mnt/share/user"),
                    ),
                ],
                json.dumps(
                    {
                        "version": "pathmapping-1.0",
                        "path_mapping_rules": [
                            {
                                "source_path_format": "WINDOWS",
                                "source_path": r"c:\Users\user",
                                "destination_path": "/mnt/share/user",
                            }
                        ],
                    }
                ),
                id="single windows",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/home/user"),
                        destination_path=PurePosixPath("/mnt/share/user"),
                    ),
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/home/user2"),
                        destination_path=PurePosixPath("/mnt/share/user2"),
                    ),
                ],
                json.dumps(
                    {
                        "version": "pathmapping-1.0",
                        "path_mapping_rules": [
                            {
                                "source_path_format": "POSIX",
                                "source_path": "/home/user",
                                "destination_path": "/mnt/share/user",
                            },
                            {
                                "source_path_format": "POSIX",
                                "source_path": "/home/user2",
                                "destination_path": "/mnt/share/user2",
                            },
                        ],
                    }
                ),
                id="multiple posix",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.WINDOWS,
                        source_path=PureWindowsPath(r"c:\Users\user"),
                        destination_path=PurePosixPath("/mnt/share/user"),
                    ),
                    PathMappingRule(
                        source_path_format=PathFormat.WINDOWS,
                        source_path=PureWindowsPath(r"c:\Users\user2"),
                        destination_path=PurePosixPath("/mnt/share/user2"),
                    ),
                ],
                json.dumps(
                    {
                        "version": "pathmapping-1.0",
                        "path_mapping_rules": [
                            {
                                "source_path_format": "WINDOWS",
                                "source_path": r"c:\Users\user",
                                "destination_path": "/mnt/share/user",
                            },
                            {
                                "source_path_format": "WINDOWS",
                                "source_path": r"c:\Users\user2",
                                "destination_path": "/mnt/share/user2",
                            },
                        ],
                    }
                ),
                id="multiple windows",
            ),
        ],
    )
    def test_materialize(
        self, rules: Optional[list[PathMappingRule]], expected_json: str, session_id: str
    ) -> None:
        # Test that Session._materialize_path_mapping works as required by the
        # schema.

        # GIVEN
        job_params = dict[str, ParameterValue]()
        env_vars = dict[str, Optional[str]]()
        symtab = SymbolTable()
        with Session(
            session_id=session_id, job_parameter_values=job_params, path_mapping_rules=rules
        ) as session:
            # WHEN
            session._materialize_path_mapping(SpecificationRevision.v2023_09, env_vars, symtab)

            # THEN
            assert symtab["Session.HasPathMappingRules"] == ("true" if rules else "false")
            assert "Session.PathMappingRulesFile" in symtab
            filename = symtab["Session.PathMappingRulesFile"]
            assert os.path.exists(filename)
            with open(filename, "r") as file:
                contents = file.read()
            assert contents == expected_json

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_run_task(self, caplog: pytest.LogCaptureFixture, session_id: str) -> None:
        # Test that path mapping rules are passed through to a running Task.
        # i.e. that run_task hooks _materialize_path_mapping() up correctly.

        # GIVEN
        # A script that just prints out some messages indicating that we got the
        # expected data.
        # Note: Testing the contents of the env var or the file on disk is overkill;
        #  we know they're okay from test_materialize() above.
        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Script }}"])
            ),
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Script",
                    type=EmbeddedFileTypes_2023_09.TEXT,
                    data="import os; print('Has: {{Session.HasPathMappingRules}}')",
                )
            ],
        )
        job_params = dict[str, ParameterValue]()
        task_params = dict[str, ParameterValue]()
        path_mapping_rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/home/user"),
                destination_path=PurePosixPath("/mnt/share/user"),
            ),
        ]
        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            path_mapping_rules=path_mapping_rules,
        ) as session:
            # WHEN
            session.run_task(step_script=script, task_parameter_values=task_params)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "Has: true" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_enter_environment(self, caplog: pytest.LogCaptureFixture, session_id: str) -> None:
        # Test that path mapping rules are passed through to a running environment-enter.
        # i.e. that enter_environment hooks _materialize_path_mapping() up correctly.

        # GIVEN
        # A script that just prints out some messages indicating that we got the
        # expected data.
        # Note: Testing the contents of the env var or the file on disk is overkill;
        #  we know they're okay from test_materialize() above.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(command=sys.executable, args=["{{ Env.File.Script }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Script",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import os; print('Has: {{Session.HasPathMappingRules}}')",
                    )
                ],
            )
        )
        job_params = dict[str, ParameterValue]()
        path_mapping_rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/home/user"),
                destination_path=PurePosixPath("/mnt/share/user"),
            ),
        ]
        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            path_mapping_rules=path_mapping_rules,
        ) as session:
            # WHEN
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "Has: true" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_exit_environment(self, caplog: pytest.LogCaptureFixture, session_id: str) -> None:
        # Test that path mapping rules are passed through to a running environment-exit.
        # i.e. that exit_environment hooks _materialize_path_mapping() up correctly.

        # GIVEN
        # A script that just prints out some messages indicating that we got the
        # expected data.
        # Note: Testing the contents of the env var or the file on disk is overkill;
        #  we know they're okay from test_materialize() above.
        environment = _environment_from_script(
            EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Script }}"])
                ),
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Script",
                        type=EmbeddedFileTypes_2023_09.TEXT,
                        data="import os; print('Has: {{Session.HasPathMappingRules}}')",
                    )
                ],
            )
        )
        job_params = dict[str, ParameterValue]()
        path_mapping_rules = [
            PathMappingRule(
                source_path_format=PathFormat.POSIX,
                source_path=PurePosixPath("/home/user"),
                destination_path=PurePosixPath("/mnt/share/user"),
            ),
        ]
        with Session(
            session_id=session_id,
            job_parameter_values=job_params,
            path_mapping_rules=path_mapping_rules,
        ) as session:
            identifier = session.enter_environment(environment=environment)

            # WHEN
            session.exit_environment(identifier=identifier)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "Has: true" in caplog.messages

    @pytest.mark.parametrize(
        "rules, given, expected",
        [
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt"),
                        destination_path=PurePosixPath("/home"),
                    )
                ],
                "/mnt/foo",
                "/home/foo",
                id="simple case",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt"),
                        destination_path=PurePosixPath("/home"),
                    ),
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt/share"),
                        destination_path=PurePosixPath("/share"),
                    ),
                ],
                "/mnt/share/foo",
                "/share/foo",
                id="applies longer source first (1)",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt/share"),
                        destination_path=PurePosixPath("/share"),
                    ),
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt"),
                        destination_path=PurePosixPath("/home"),
                    ),
                ],
                "/mnt/share/foo",
                "/share/foo",
                id="applies longer source first (2)",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt/share"),
                        destination_path=PurePosixPath("/mnt"),
                    ),
                    PathMappingRule(
                        source_path_format=PathFormat.POSIX,
                        source_path=PurePosixPath("/mnt"),
                        destination_path=PurePosixPath("/home"),
                    ),
                ],
                "/mnt/share/foo",
                "/mnt/foo",
                id="apply only one rule",
            ),
            pytest.param(
                [
                    PathMappingRule(
                        source_path_format=PathFormat.WINDOWS,
                        source_path=PureWindowsPath(r"D:\Assets"),
                        destination_path=PurePosixPath("/tmp/openjd"),
                    ),
                ],
                r"D:\Assets\Scene\Output",
                "/tmp/openjd/Scene/Output",
                id="windows source path",
            ),
        ],
    )
    def test_params_are_path_mapped(
        self, rules: list[PathMappingRule], given: str, expected: str
    ) -> None:
        # A test that PATH type parameters are path mapped as expected.

        # GIVEN
        params = {
            "Path": ParameterValue(type=ParameterValueType.PATH, value=given),
            "String": ParameterValue(type=ParameterValueType.STRING, value=given),
        }
        with Session(
            session_id="test", job_parameter_values=params, path_mapping_rules=rules
        ) as session:
            # WHEN
            with patch(f"{path_mapping_impl_mod.__name__}.os_name", "posix"):
                symtab = session._symbol_table(SpecificationRevision.v2023_09, params)

        # THEN
        assert symtab["RawParam.Path"] == given
        assert symtab["Param.Path"] == expected
        assert symtab["RawParam.String"] == given
        assert symtab["Param.String"] == given
        assert symtab["Task.RawParam.Path"] == given
        assert symtab["Task.Param.Path"] == expected
        assert symtab["Task.RawParam.String"] == given
        assert symtab["Task.Param.String"] == given


SOME_ENV_VARS = {
    "FOO": "bar",
    "BAZ": "qux",
}


class TestSimplifiedEnvironmentVariableChanges:
    def test_init(self) -> None:
        # WHEN
        initial_vars = dict(SOME_ENV_VARS, **{"Asdf": "jkl;"})
        simplified = SimplifiedEnvironmentVariableChanges(initial_vars)

        # THEN
        if is_windows():
            assert simplified._to_set == dict(SOME_ENV_VARS, **{"ASDF": "jkl;"})
        else:
            assert simplified._to_set == initial_vars

    @pytest.mark.parametrize(
        "changes,set_result",
        [
            pytest.param([], SOME_ENV_VARS, id="No changes"),
            pytest.param(
                [EnvironmentVariableSetChange("QUUX", "corge")],
                dict(SOME_ENV_VARS, **{"QUUX": "corge"}),
                id="Set new variable",
            ),
            pytest.param(
                [EnvironmentVariableSetChange("Quux", "corge")],
                (
                    dict(SOME_ENV_VARS, **{"QUUX": "corge"})
                    if is_windows()
                    else dict(SOME_ENV_VARS, **{"Quux": "corge"})
                ),
                id="Set new variable mixed case",
            ),
            pytest.param(
                [EnvironmentVariableSetChange("FOO", "corge")],
                {"FOO": "corge", "BAZ": "qux"},
                id="Modify existing variable",
            ),
            pytest.param(
                [EnvironmentVariableUnsetChange("QUUX")],
                dict(SOME_ENV_VARS, **{"QUUX": None}),
                id="Unset new variable",
            ),
            pytest.param(
                [EnvironmentVariableUnsetChange("FOO")],
                {"BAZ": "qux", "FOO": None},
                id="Unset existing variable",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("QUUX", "corge"),
                    EnvironmentVariableUnsetChange("QUUX"),
                ],
                dict(SOME_ENV_VARS, **{"QUUX": None}),
                id="Add then unset new",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("QUUX", "corge"),
                    EnvironmentVariableSetChange("QUUX", "grault"),
                ],
                dict(SOME_ENV_VARS, **{"QUUX": "grault"}),
                id="Add then change new",
            ),
            pytest.param(
                [
                    EnvironmentVariableUnsetChange("QUUX"),
                    EnvironmentVariableSetChange("QUUX", "corge"),
                ],
                dict(SOME_ENV_VARS, **{"QUUX": "corge"}),
                id="Unset then add new",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("QUUX", "corge"),
                    EnvironmentVariableUnsetChange("QUUX"),
                    EnvironmentVariableSetChange("QUUX", "grault"),
                ],
                dict(SOME_ENV_VARS, **{"QUUX": "grault"}),
                id="Add then unset then add new",
            ),
            pytest.param(
                [
                    EnvironmentVariableUnsetChange("FOO"),
                    EnvironmentVariableSetChange("FOO", "corge"),
                    EnvironmentVariableUnsetChange("FOO"),
                ],
                {"BAZ": "qux", "FOO": None},
                id="Unset then add then unset existing",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("FOO", "corge"),
                    EnvironmentVariableUnsetChange("FOO"),
                ],
                {"BAZ": "qux", "FOO": None},
                id="Change then unset existing",
            ),
            pytest.param(
                [
                    EnvironmentVariableUnsetChange("FOO"),
                    EnvironmentVariableSetChange("FOO", "corge"),
                ],
                {"FOO": "corge", "BAZ": "qux"},
                id="Unset then add existing",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("FOO", "corge"),
                    EnvironmentVariableUnsetChange("FOO"),
                    EnvironmentVariableSetChange("FOO", "grault"),
                ],
                {"FOO": "grault", "BAZ": "qux"},
                id="Change then unset then add existing",
            ),
            pytest.param(
                [
                    EnvironmentVariableSetChange("Foo", "corge"),
                    EnvironmentVariableUnsetChange("FOo"),
                    EnvironmentVariableSetChange("foo", "grault"),
                ],
                (
                    {"FOO": "grault", "BAZ": "qux"}
                    if is_windows()
                    else dict(SOME_ENV_VARS, **{"Foo": "corge", "FOo": None, "foo": "grault"})  # type: ignore
                ),
                id="Change then unset then add existing mixed case",
            ),
        ],
    )
    def test_simplify_ordered_changes_happy_path(
        self,
        changes: list[EnvironmentVariableChange],
        set_result: dict[str, Optional[str]],
    ) -> None:
        # GIVEN
        simplified = SimplifiedEnvironmentVariableChanges(SOME_ENV_VARS)

        # WHEN
        simplified.simplify_ordered_changes(changes)

        # THEN
        assert simplified._to_set == set_result

    @staticmethod
    @pytest.fixture
    def fix_simple_unset_foo() -> SimplifiedEnvironmentVariableChanges:
        empty_str_dict: dict[str, str] = dict()
        simplified = SimplifiedEnvironmentVariableChanges(empty_str_dict)
        simplified.simplify_ordered_changes([EnvironmentVariableUnsetChange("FOO")])
        return simplified

    @staticmethod
    @pytest.fixture
    def fix_simple_foo_to_corge() -> SimplifiedEnvironmentVariableChanges:
        return SimplifiedEnvironmentVariableChanges({"FOO": "corge"})

    @pytest.mark.parametrize(
        "input_vars,simplified_changes_fixture,result_vars",
        [
            pytest.param(
                {},
                "fix_simple_unset_foo",
                {"FOO": None},
                id="Remove new variable",
            ),
            pytest.param(
                {"FOO": "bar"},
                "fix_simple_unset_foo",
                {"FOO": None},
                id="Remove existing variable",
            ),
            pytest.param(
                {"FOO": "bar"},
                "fix_simple_foo_to_corge",
                {"FOO": "corge"},
                id="Change existing variable",
            ),
            pytest.param(
                {},
                "fix_simple_foo_to_corge",
                {"FOO": "corge"},
                id="Add new variable",
            ),
            pytest.param(
                {"Foo": "bar"},
                "fix_simple_foo_to_corge",
                {"FOO": "corge"} if is_windows() else {"Foo": "bar", "FOO": "corge"},
                id="Mixed Case",
            ),
        ],
    )
    def test_apply_to_environment(
        self,
        input_vars: dict[str, Optional[str]],
        simplified_changes_fixture: str,
        result_vars: dict[str, Optional[str]],
        request: pytest.FixtureRequest,
    ) -> None:
        # GIVEN
        simplified_changes: SimplifiedEnvironmentVariableChanges = request.getfixturevalue(
            simplified_changes_fixture
        )

        # WHEN
        simplified_changes.apply_to_environment(input_vars)

        # THEN
        assert input_vars == result_vars


class TestEnvironmentVariablesInTasks_2023_09:
    """Tests that environment variables defined in Environments are properly made available
    when running tasks in the same session."""

    @staticmethod
    @pytest.fixture
    def step_script_definition() -> StepScript_2023_09:
        return StepScript_2023_09(
            embeddedFiles=[
                EmbeddedFileText_2023_09(
                    name="Run",
                    type="TEXT",
                    runnable=True,
                    data='import os; print(f\'FOO={os.environ.get("FOO", "FOO-not-set")}\'); print(f\'BAR={os.environ.get("BAR", "BAR-not-set")}\');',
                )
            ],
            actions=StepActions_2023_09(
                onRun=Action_2023_09(command=sys.executable, args=["{{ Task.File.Run }}"])
            ),
        )

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_direct_definition(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment's definition directly defines environment variables,
        # then those variables are available in the Task.

        # GIVEN
        environment = Environment_2023_09(
            name="Env", variables={"FOO": "FOO-value", "BAR": "BAR-value"}
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-value" in caplog.messages
            assert "BAR=BAR-value" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_redefinition_nested(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when one environement redefines an env var that a previous one defined,
        # then we get the override.

        # GIVEN
        environment_outer = Environment_2023_09(
            name="Env", variables={"FOO": "FOO-value", "BAR": "BAR-value"}
        )
        environment_inner = Environment_2023_09(
            name="Env", variables={"FOO": "FOO-override", "BAR": "BAR-override"}
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment_outer)
            session.enter_environment(environment=environment_inner)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-override" in caplog.messages
            assert "FOO=FOO-value" not in caplog.messages
            assert "BAR=BAR-override" in caplog.messages
            assert "BAR=BAR-value" not in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_redefinition_exit(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when one environement redefines an env var that a previous one defined,
        # and then we exit that environment then the running task gets the values from the
        # outer environment.

        # GIVEN
        environment_outer = Environment_2023_09(
            name="Env",
            variables={"FOO": "FOO-value", "BAR": "BAR-value"},
            script=EnvironmentScript_2023_09(
                embeddedFiles=[
                    EmbeddedFileText_2023_09(
                        name="Run",
                        type="TEXT",
                        runnable=True,
                        data="import os; print(f'FOO={os.environ.get(\"FOO\")}'); print(f'BAR={os.environ.get(\"BAR\")}');",
                    )
                ],
                actions=EnvironmentActions_2023_09(
                    onExit=Action_2023_09(command=sys.executable, args=["{{ Env.File.Run }}"])
                ),
            ),
        )
        environment_inner = Environment_2023_09(
            name="Env", variables={"FOO": "FOO-override", "BAR": "BAR-override"}
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            outer_env_id = session.enter_environment(environment=environment_outer)
            inner_env_id = session.enter_environment(environment=environment_inner)
            session.exit_environment(identifier=inner_env_id)

            # WHEN
            session.exit_environment(identifier=outer_env_id)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-override" not in caplog.messages
            assert "FOO=FOO-value" in caplog.messages
            assert "BAR=BAR-override" not in caplog.messages
            assert "BAR=BAR-value" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_stdout(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables via a stdout handler then the variable
        # is available in the Task.

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_env: FOO=FOO-value')"]
                    )
                )
            ),
            variables={"BAR": "BAR-value"},
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-value" in caplog.messages
            assert "BAR=BAR-value" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_multi_line_nonvalid_json_stdout(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables via a stdout handler
        # as a multiline json it is processed properly

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable,
                        args=["-c", "import json; print('openjd_env: \"FOO=12\\\\n34')"],
                    )
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            assert (
                'openjd_env: "FOO=12\\n34 -- ERROR: Unterminated string starting at: line 1 column 1 (char 0)'
                in caplog.messages
            )

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_multi_line_stdout(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Test that when an environment defines variables via a stdout handler
        # as a multiline json it is processed properly

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable,
                        args=["-c", "print('openjd_env: \"FOO=12\\\\n34\"')"],
                    )
                ),
            ),
        )

        script = StepScript_2023_09(
            actions=StepActions_2023_09(
                onRun=Action_2023_09(
                    command=sys.executable,
                    args=[
                        "-c",
                        "import os; print('FOO:'); print(f'{os.environ[\"FOO\"]}'); print('---')",
                    ],
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)
            # WHEN
            session.run_task(
                step_script=script,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO:" in caplog.messages
            assert "12" in caplog.messages
            assert "34" in caplog.messages
            assert "---" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_stdout_overrides_direct(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables directly, and then redefines them via a stdout
        # handler then the variable from the stdout handler takes precidence.

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_env: FOO=FOO-value')"]
                    )
                )
            ),
            variables={"BAR": "BAR-value", "FOO": "NOT FOO"},
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-value" in caplog.messages
            assert "BAR=BAR-value" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_stdout_set_empty_json(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables directly and as empty json it is processed properly

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_env: \"FOO=\"')"]
                    )
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_stdout_set_empty(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables directly and as empty string it is processed properly

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_env: FOO=')"]
                    )
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=" in caplog.messages

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_def_via_stdout_fails_session_action_on_error(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment defines variables that are malformed then session fails

        # GIVEN
        environment = Environment_2023_09(
            name="Env",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_env: FOO')"]
                    )
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        callback = MagicMock()
        with Session(
            session_id=session_id, job_parameter_values=job_params, callback=callback
        ) as session:
            # WHEN
            session.enter_environment(environment=environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert session.state == SessionState.READY_ENDING
            assert (
                "openjd_env: FOO -- ERROR: Failed to parse environment variable assignment."
                in caplog.messages
            )

        callback.assert_has_calls(
            [
                call.__bool__(),
                call(
                    session_id,
                    ActionStatus(
                        state=ActionState.RUNNING,
                        progress=None,
                        status_message=None,
                        fail_message=None,
                        exit_code=None,
                    ),
                ),
                call.__bool__(),
                call(
                    session_id,
                    ActionStatus(
                        state=ActionState.FAILED,
                        progress=None,
                        status_message=None,
                        fail_message="Failed to parse environment variable assignment.",
                        exit_code=ANY,
                    ),
                ),
            ]
        )

    @pytest.mark.usefixtures("caplog")  # builtin fixture
    def test_undef_via_stdout(
        self, caplog: pytest.LogCaptureFixture, step_script_definition: StepScript_2023_09
    ) -> None:
        # Test that when an environment unsets an environment variable that it is actually not defined in
        # the task that's run.

        # GIVEN
        outer_environment = Environment_2023_09(
            name="EnvOuter",
            variables={"BAR": "BAR-value", "FOO": "FOO-value"},
        )
        inner_environment = Environment_2023_09(
            name="EnvInner",
            script=EnvironmentScript_2023_09(
                actions=EnvironmentActions_2023_09(
                    onEnter=Action_2023_09(
                        command=sys.executable, args=["-c", "print('openjd_unset_env: FOO')"]
                    )
                )
            ),
        )
        session_id = uuid.uuid4().hex
        job_params = dict[str, ParameterValue]()
        with Session(session_id=session_id, job_parameter_values=job_params) as session:
            session.enter_environment(environment=outer_environment)
            session.enter_environment(environment=inner_environment)
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # WHEN
            session.run_task(
                step_script=step_script_definition,
                task_parameter_values=dict[str, ParameterValue](),
            )
            while session.state == SessionState.RUNNING:
                time.sleep(0.1)

            # THEN
            assert "FOO=FOO-not-set" in caplog.messages
            assert "BAR=BAR-value" in caplog.messages
