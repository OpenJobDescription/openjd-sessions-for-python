# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# As app_20s_run.py except it does not exit when it gets a SIGTERM/SIGBREAK

import signal
import sys
import time


def hook(handle, frame):
    print("Trapped")
    sys.stdout.flush()


if sys.platform.startswith("win"):
    signal.signal(signal.SIGBREAK, hook)
    signal.signal(signal.SIGINT, hook)
else:
    signal.signal(signal.SIGTERM, hook)

for i in range(0, 20):
    print(i)
    sys.stdout.flush()
    time.sleep(1)
