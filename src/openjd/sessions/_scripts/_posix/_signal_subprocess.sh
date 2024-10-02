#!/bin/sh -x
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Usage:
#  $0 <pid> <signal> <signal_child> <all_subprocesses>
#  where:
#    <pid> PID of the parent process to the one to signal
#    <signal> is compatible with the `-s` option of /bin/kill
#    <signal_child> must be "True" or "False"; if True then
#       we instead signal the child of <pid> but not <pid> 
#    <all_subprocesses> must be "True" or "False"


# Note: A limitation of this implementation is that it will only sigkill
# processes that are in the same process-group as the command that we ran.
# In the future, we can extend this to killing all processes spawned (including into
# new process-groups since the parent-pid will allow the mapping)
# by a depth-first traversal through the children. At each recursive
# step we:
#  1. SIGSTOP the process, so that it cannot create new subprocesses;
#  2. Recurse into each child; and
#  3. SIGKILL the process.
# Things to watch for when doing so:
#  a. PIDs can get reused; just because a pid was a child of a process at one point doesn't
#     mean that it's still the same process when we recurse to it. So, check that the parent-pid
#     of any child is still as expected before we signal it or collect its children.
#  b. When we run the command using `sudo` then we need to either run code that does the whole
#     algorithm as the other user, or `sudo` to send every process signal.

set -x

PID="$1"
SIG="$2"

[ -f /bin/kill ] && KILL=/bin/kill
[ ! -n "${KILL:-}" ] && [ -f /usr/bin/kill ] && KILL=/usr/bin/kill

if [ ! -n "${KILL:-}" ]
then
    echo "ERROR - Could not find the 'kill' command under /bin or /usr/bin. Please install it."
    exit 1
fi

exec "$KILL" -s "$SIG" -- "$PID"
