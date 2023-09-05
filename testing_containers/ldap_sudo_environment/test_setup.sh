#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

set -eu

cp /config/run_tests.sh /home/hostuser
chown hostuser:hostuser /home/hostuser/run_tests.sh
