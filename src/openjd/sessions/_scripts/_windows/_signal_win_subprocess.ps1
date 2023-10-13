param(
    [int]$P_ID,
    [string]$SIG,
    [string]$SIGNAL_CHILD = "False",
    [string]$INCL_SUBPROCS = "False"
)

# Note: SIGNAL_CHILD is used for impersonation. This is not implemented yet.

if ($SIG -eq "kill") {
    if ($INCL_SUBPROCS -eq "True") {
        taskkill /pid $P_ID /f /t
    } else {
        taskkill /pid $P_ID /f
    }
}

exit $LASTEXITCODE
