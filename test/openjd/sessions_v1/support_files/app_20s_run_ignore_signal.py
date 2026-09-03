# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# As app_20s_run.py except it does not exit when it gets a SIGTERM/SIGBREAK

import os
import signal
import sys
import time


def hook(handle, frame):
    # os.write(2) rather than print() + flush(): a signal handler can interrupt
    # the main loop *inside* sys.stdout.flush(), and re-entering the same
    # BufferedWriter raises
    #   RuntimeError: reentrant call inside <_io.BufferedWriter name='<stdout>'>
    # which loses this message and exits non-zero. write(2) is unbuffered, so it
    # cannot re-enter. Every print here is flushed immediately, so nothing is
    # sitting in the buffer for this to appear out of order with.
    os.write(1, b"Trapped\n")


if sys.platform.startswith("win"):
    signal.signal(signal.SIGBREAK, hook)
    signal.signal(signal.SIGINT, hook)
else:
    signal.signal(signal.SIGTERM, hook)

for i in range(0, 20):
    print(i)
    sys.stdout.flush()
    time.sleep(1)
