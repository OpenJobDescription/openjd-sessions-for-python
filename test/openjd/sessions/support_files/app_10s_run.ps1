# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

param (
    [string]$Python
)

$Script = Join-Path $PSScriptRoot "app_10s_run.py"

& $Python $Script
exit $LASTEXITCODE