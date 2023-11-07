# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import pytest
import subprocess

from openjd.sessions._os_checker import is_windows
from openjd.sessions._powershell_generator import (
    generate_exit_code_powershell,
    generate_exit_code_wrapper,
    encode_to_base64,
    replace_escapes,
)


class TestPowershellGenerator:
    @pytest.mark.skipif(
        not is_windows(), reason="This test will be only run in Windows for checking the exit code"
    )
    def test_generate_exit_code_powershell(self):
        cmd = "exit 123"
        ps_script = generate_exit_code_powershell(cmd)
        process = subprocess.Popen(
            ["powershell", "-Command", ps_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        process.communicate()
        assert process.returncode == 123

    def test_generate_exit_code_powershell_no_run(self):
        cmd = "Start-Process powershell.exe"
        result = generate_exit_code_powershell(cmd)
        assert "$process = Start-Process powershell.exe" in result
        assert "exit $process.ExitCode" in result

    def test_generate_exit_code_wrapper(self):
        args = ["Start-Process", "powershell.exe"]
        result = generate_exit_code_wrapper(args)
        assert "& 'Start-Process' 'powershell.exe'" in result
        assert "exit $LASTEXITCODE" in result

    def test_generate_exit_code_wrapper_with_replacement(self):
        args = ["python", "-c", "print('hi')"]
        result = generate_exit_code_wrapper(args)
        print(result)
        assert "& 'python' '-c' 'print(''hi'')'" in result
        assert "exit $LASTEXITCODE" in result

    @pytest.mark.parametrize(
        "input_str, expected_output",
        [("test", "dABlAHMAdAA="), ("hello", "aABlAGwAbABvAA=="), ("world", "dwBvAHIAbABkAA==")],
    )
    def test_encode_to_base64_parametrized(self, input_str, expected_output):
        result = encode_to_base64(input_str)
        assert result == expected_output

    def test_replace_escapes(self):
        result = replace_escapes("This is a test string with \\, \" and ' characters.")
        assert result == "This is a test string with \\\\, \" and '' characters."
