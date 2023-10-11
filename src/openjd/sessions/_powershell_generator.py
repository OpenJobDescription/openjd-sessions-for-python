# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from typing import Sequence, Optional
import base64
import re
from getpass import getuser

from ._session_user import WindowsSessionUser


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
        # This is a known issue in PowerShell without installing additional `Native` module
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


def generate_start_job_wrapper(
    args: Sequence[str], user: Optional[WindowsSessionUser] = None
) -> str:
    """Generate a wrapper outside the command used for executing as a background job.
    For the background job, it can only return string. Exit code cannot be returned. Therefore, we need to handle the
    return value and extract the exit code. Inside the job script, the script will return the string
    `Open Job Description action exits with code: [exit_code]`, and the [exit_code] will be captured by using the
    regular expression in order to return exit code to the main process correctly.

    Args:
        args: Command arguments passed by users.
        user: User used for impersonation. If it is None, the script will run under current user.

    Returns:
        A PowerShell Script contains the user's commands with the Start-Job code wrapper.
    """
    cmd_script = generate_exit_code_wrapper(
        args, exit_action='return "`nOpen Job Description action exits with code: "+'
    )

    credential_argument = ""
    if user and user.user != getuser():
        credential_argument = (
            f" -Credential (New-Object -TypeName System.Management.Automation.PSCredential"
            f' -ArgumentList "{user.user}", '
            f"([System.Environment]::GetEnvironmentVariable("
            f'"{user.user}", [System.EnvironmentVariableTarget]::User)  | ConvertTo-SecureString))'
        )

    return f"""function ProcessJobOutput {{
    param(
        [Parameter(Mandatory=$true)]
        [System.Management.Automation.Job]$RunningJob
    )
    try{{
        $jobOutput = Receive-Job -Job $RunningJob -ErrorAction Stop
        if ($null -ne $jobOutput) {{
            $pattern = 'Open Job Description action exits with code: (\\d+)'
            $regex = [System.Text.RegularExpressions.Regex]::Match($jobOutput, $pattern)
            if ($regex.Success) {{
                # TODO: Need to ignore the return message but print rest of logging
                $global:exitCode = $regex.Groups[1].Value
            }}
            Write-Output $jobOutput
        }}
    }} catch {{
        Write-Output "Error: $_"
    }}
}}

# If the job finish without error, a new value must be assigned to the exit code.
# If no value is assigned, there must be an error inside the job, the script will exit with 1
$global:exitCode = 1
$job = Start-Job -ScriptBlock {{
    {cmd_script}
}}{credential_argument};
do {{
    # Get the output that's currently available from the job
    ProcessJobOutput -RunningJob $job
    # Check the job's state
    $jobState = (Get-Job -Id $job.Id).State
    Start-Sleep -Seconds 0.2
}} while ($jobState -eq 'Running')
Wait-Job $job;
ProcessJobOutput -RunningJob $job
Remove-Job $job;
exit $global:exitCode
"""
