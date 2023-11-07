# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from typing import Sequence, Optional
import base64
import os
import re

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
        # To include a single quotation mark in a single-quoted string, use a second consecutive single quote.
        # https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules
        "'": "''",
    }
    return re.sub(r"['\\]", lambda m: replacements[m.group(0)], args)


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


def generate_process_wrapper(
    args: Sequence[str],
    signal_script: os.PathLike,
    user: Optional[WindowsSessionUser] = None,
) -> str:
    """Generate a wrapper outside the command used for executing as a process.

    Args:
        args: Command arguments passed by users.
        user: User used for impersonation. If it is None, the script will run under current user.

    Returns:
        A PowerShell Script contains the user's commands with the Process code wrapper.
    """

    cmd = args[0]
    quoted_args = [f"{replace_escapes(arg)}" for arg in args[1:]]
    arg_list = ""
    for arg in quoted_args:
        arg_list += f"$pInfo.ArgumentList.Add('{arg}')\r\n"

    credential_info = ""
    if user and not user.is_process_user():
        # To include a single quotation mark in a single-quoted string, need to use a second consecutive single quote.
        # https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules
        username = user.user.replace("'", "''")
        if user.password:
            password = user.password.replace("'", "''")
        else:
            password = ""
        credential_info = f"""
$pInfo.UserName = '{username}'
$pInfo.Password = ('{password}' | ConvertTo-SecureString -AsPlainText -Force)
$pInfo.CreateNoWindow = $true
"""

    return f"""
$pInfo = New-Object System.Diagnostics.ProcessStartInfo

$pInfo.FileName = '{cmd}'
{arg_list}
$pInfo.RedirectStandardOutput = $true
$pInfo.RedirectStandardError = $true
$pInfo.UseShellExecute = $false
{credential_info}

$p = New-Object System.Diagnostics.Process
$p.StartInfo = $pInfo

$WriteOutputAction = {{ [Console]::WriteLine($Event.SourceEventArgs.Data) }}
$WriteErrorAction = {{ [Console]::WriteLine("Error: " + $Event.SourceEventArgs.Data) }}
Register-ObjectEvent -InputObject $p -EventName OutputDataReceived -Action $WriteOutputAction | Out-Null
Register-ObjectEvent -InputObject $p -EventName ErrorDataReceived -Action $WriteErrorAction | Out-Null

$exitCode = $null

try {{
    $p.Start() | Out-Null

    $p.BeginOutputReadLine()
    $p.BeginErrorReadLine()

    while (-Not $p.HasExited) {{
        [Console]::Out.Flush()
        [Console]::Error.Flush()
        Start-Sleep 0.5
    }}
}}
catch {{
    Write-Output "Error: $_"
    $exitCode = 1
}}
finally {{
    if (-Not $p.HasExited) {{
        # If we got here before process is done, generate a ctrl-break event to the process
        $j = Start-Job -ScriptBlock {{ {signal_script} $args[0] }} -ArgumentList $p.id
        Receive-Job -Job $j -Wait -AutoRemoveJob | Write-Host

        $p.WaitForExit()
        Start-Sleep 1
    }}

    if ($exitCode -eq $null) {{ $exitCode = $p.ExitCode }}
    $p.Close()
    exit $ExitCode
 }}
"""
