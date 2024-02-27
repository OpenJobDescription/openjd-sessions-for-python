# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
import subprocess
import time
from pathlib import Path

proc = subprocess.Popen(
    args=[
        sys.executable,
        str(Path(__file__).parent / "app_20s_run.py"),
    ],
    stdout=subprocess.PIPE,
    stdin=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
    encoding="utf-8",
)

if proc.stdout is not None:
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip("\n\r")
        print(line)
        sys.stdout.flush()

for i in range(0, 20):
    print(f"Log from runner {str(i)}")
    sys.stdout.flush()
    time.sleep(1)
