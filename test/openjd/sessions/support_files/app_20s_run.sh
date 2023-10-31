#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

PYTHON="$1"

SCRIPT=$(dirname $0)/app_20s_run.py

"$PYTHON" "$SCRIPT"
exit $?