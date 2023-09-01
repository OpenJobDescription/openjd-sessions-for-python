# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

FROM python:3.9-buster

# Let our tests know that we"re in an environment that can run the sudo
# tests
ENV OPENJD_TEST_SUDO_TARGET_USER=targetuser
ENV OPENJD_TEST_SUDO_SHARED_GROUP=sharedgroup
ENV OPENJD_TEST_SUDO_DISJOINT_USER=disjointuser
ENV OPENJD_TEST_SUDO_DISJOINT_GROUP=disjointgroup

# Use a docker volume to mount the root of the repo to this directory
WORKDIR /code

# We set up two users for our tests:
#  1) hostuser -- the user that will be running the pytests.
#  2) targetuser -- the user that we'll be running subprocesses as in the
#     the cross-account tests.
#  3) disjointuser -- a user used in temporary directory creation tests.
# These accounts belong to the following groups:
#   hostuser: hostuser, sharedgroup
#   targetuser: targetuser, sharedgroup
#   disjointuser: disjointuser, disjointgroup
RUN apt-get update && apt-get install sudo && \
    rm -rf /var/lib/apt/lists/* && \
    addgroup ${OPENJD_TEST_SUDO_SHARED_GROUP} &&  \
    useradd -ms /bin/bash -G ${OPENJD_TEST_SUDO_SHARED_GROUP} ${OPENJD_TEST_SUDO_TARGET_USER} && \
    useradd -ms /bin/bash -G ${OPENJD_TEST_SUDO_SHARED_GROUP} hostuser && \
    echo "hostuser ALL=(${OPENJD_TEST_SUDO_TARGET_USER},hostuser) NOPASSWD: ALL" > /etc/sudoers.d/hostuser && \
    addgroup ${OPENJD_TEST_SUDO_DISJOINT_GROUP} && \
    useradd -ms /bin/bash -G ${OPENJD_TEST_SUDO_DISJOINT_GROUP} ${OPENJD_TEST_SUDO_DISJOINT_USER} && \
    chmod 777 /code

WORKDIR /home/hostuser

COPY --chown=hostuser:hostuser run_tests.sh /home/hostuser/

USER hostuser

CMD ["/bin/sh", "-c", "./run_tests.sh"]