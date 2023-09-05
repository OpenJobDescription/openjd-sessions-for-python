#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

PYTHON="$1"

SCRIPT=$(dirname $0)/app_10s_run.py

"$PYTHON" "$SCRIPT"
exit $?