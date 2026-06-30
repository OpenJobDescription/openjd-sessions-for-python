# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# A simple app for use in testing of the LoggingSubprocess.
# Prints out an increasing series of integers (0, 1, 2, ...)
# every second for 10 seconds
#
# Hook SIGTERM (posix) or CTRL_BREAK_EVENT (windows) and print "Trapped"
# and exit if we get the signal

import signal
import sys
import time


def hook(handle, frame):
    print("Trapped")
    sys.stdout.flush()
    sys.exit(1)


if sys.platform.startswith("win"):
    signal.signal(signal.SIGBREAK, hook)
    signal.signal(signal.SIGINT, hook)
else:
    signal.signal(signal.SIGTERM, hook)

for i in range(0, 20):
    print(f"Log from test {str(i)}")
    sys.stdout.flush()
    time.sleep(1)
