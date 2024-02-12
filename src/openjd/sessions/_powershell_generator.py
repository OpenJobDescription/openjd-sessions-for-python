# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from typing import Sequence, Optional
import base64
import re


def replace_escapes(args: str) -> str:
    """Replace special characters in a given string.

    Parameters:
    - s (str): The input string to be processed.

    Returns:
    - str: The string with the special characters replaced.
    """
    replacements = {
        # Apply the backslash characters rules from CommandLineToArgvW
        # https://learn.microsoft.com/en-us/archive/blogs/twistylittlepassagesallalike/everyone-quotes-command-line-arguments-the-wrong-way
        "\\": "\\\\",
        # This is a known issue in PowerShell 5 without installing additional `Native` module
        # https://github.com/MicrosoftDocs/PowerShell-Docs/issues/2361
        '"': '\\"',
        # To include a single quotation mark in a single-quoted string, use a second consecutive single quote.
        # https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules
        "'": "''",
    }
    return re.sub(r'[\'\\"]', lambda m: replacements[m.group(0)], args)


def generate_exit_code_powershell(cmd_line: str) -> str:
    """Generate a PowerShell script string that runs the provided command and then exits with
    the command's exit code. This can be useful when needing to propagate the exit code of a
    PowerShell Script subprocess to the parent process.

    Args:
        cmd_line (str): The command line string that needs to be executed in the generated PowerShell script.

    Returns:
        str: A PowerShell script string that executes the provided command and then exits with its exit code.
    """
    return f"""
$process = {cmd_line}
exit $process.ExitCode
"""


def generate_exit_code_wrapper(args: Sequence[str], exit_action: Optional[str] = None) -> str:
    """Generate a wrapper outside the command used for catching the exit code and return it correctly.

    If the command is not found or the exit code is $null, exit code will be returned as 1.

    Args:
        args: command arguments passed by users.
        special_character_replace_fn: A function used for replacing particular characters in the command.
        exit_action: A string inserted to the wrapper to determine what the action is when the task is done.
            If it is None, the script will exit with the exit code.

    Returns:
        str: A PowerShell Script contains the user's commands with the exit code wrapper.
    """
    quoted_args = [f"'{replace_escapes(arg)}'" for arg in args]
    exit_action = exit_action if exit_action else "exit"
    cmd_line = " ".join(quoted_args)
    return f"""
try {{
    # Attempt to run the command. This will fail if the command is not recognized.
    & {cmd_line}
}}
catch [System.Management.Automation.CommandNotFoundException] {{
    Write-Host "Command not found: $_, exiting with code 1."
    {exit_action} 1
}}
catch {{
    Write-Host "An unexpected error occurred: $_"
    if ($LASTEXITCODE -eq $null) {{
        Write-Host "The original exit code is null. Exit with code = 1"
        {exit_action} 1  # Exit with code 1 if command does not set an exit code
    }}
    {exit_action} $LASTEXITCODE
}}

{exit_action} $LASTEXITCODE
"""


def encode_to_base64(command: str) -> str:
    """Encodes the provided string command to a base64 string using UTF-16LE encoding.
    This method is useful when needing an encoding equivalent to PowerShell's Unicode encoding.

    Args:
        command (str): The original command string needed to be encoded.

    Returns:
        str: The base64 encoded version of the input command using UTF-16LE encoding.
    """
    command_bytes = command.encode("utf-16le")
    encoded_command = base64.b64encode(command_bytes)
    return encoded_command.decode("utf-8")
