param([Parameter(Mandatory=$true)][int32]$proc_id)
function Send-SIGBREAK {
    # Geneates a console CTRL_EVENT_BREAK signal to a process group id.
    param (
        [int]$pgid
    )

    Add-Type -TypeDefinition @"
        using System;
        using System.Runtime.InteropServices;
        using System.ComponentModel;

        public class WinConsoleAPIWrapper {
            [DllImport("kernel32.dll", SetLastError = true, ExactSpelling = true)]
            public static extern bool AttachConsole(uint dwProcessId);

            [DllImport("kernel32.dll", SetLastError = true, ExactSpelling = true)]
            public static extern bool FreeConsole();

            [DllImport("kernel32.dll", SetLastError = true, ExactSpelling = true)]
            public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);

            public delegate bool ConsoleCtrlDelegate(int dwCtrlType);

            [DllImport("kernel32.dll", SetLastError = true, ExactSpelling = true)]
            public static extern uint GetLastError();

            public static string GetLastErrorMessage() {
                uint errorCode = GetLastError();
                return new Win32Exception((int)errorCode).Message;
            }
        }
"@ -Language CSharp

    #  Detach from current console
    if (-not [WinConsoleAPIWrapper]::FreeConsole()) {
        Write-Error "ERROR - Failed to free console: $([WinConsoleAPIWrapper]::GetLastErrorMessage())"
        return
    }

    #  Attach the calling process to the console of the specified process.
    if (-not [WinConsoleAPIWrapper]::AttachConsole($pgid)) {
        Write-Error "ERROR - Failed to attach console: $([WinConsoleAPIWrapper]::GetLastErrorMessage())"
        return
    }

    if (-not [WinConsoleAPIWrapper]::GenerateConsoleCtrlEvent(1, $pgid)) {
        Write-Error "ERROR - Failed to generate console ctrl event: $([WinConsoleAPIWrapper]::GetLastErrorMessage())"
        return
    }

    Write-Host "Successfully sent SIGBREAK to process: $pgid"
}


Send-SIGBREAK "$proc_id"