// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

// This is a minimal C program that sleeps until it receives a SIGTERM signal
// and outputs the process ID of the sender.

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bool received_signal = false;

static void signal_handler(int sig, siginfo_t *siginfo, void *context) {
    // Output PID of the sending process
    int pid_of_sending_process = (int) siginfo->si_pid;
    printf("%d\n", pid_of_sending_process);

    // Tell main loop to exit
    received_signal = true;
}

int main(int argc, char *argv[]) {
    // register signal handler
    struct sigaction signal_action;
    signal_action.sa_sigaction = *signal_handler;
    // get details about the signal
    signal_action.sa_flags |= SA_SIGINFO;
    if(sigaction(SIGTERM, &signal_action, NULL) != 0) {
        printf("Could not register signal handler\n");
        return errno;
    }

    // sleep until SIGINT received
    while(!received_signal) {
        sleep(1);
    }

    return 0;
}
