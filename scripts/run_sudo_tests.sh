#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

set -eux

# Run this from the root of the repository
if ! test -d scripts
then
    echo "Must run from the root of the repository"
    exit 1
fi

USE_LDAP="False"
BUILD_ONLY="False"
while [[ "${1:-}" != "" ]]; do
    case $1 in
        -h|--help)
            echo "Usage: run_sudo_tests.sh [--build]"
            exit 1
            ;;
        --ldap)
            echo "Using the LDAP client container image for testing."
            USE_LDAP="True"
            ;;
        --build-only)
            BUILD_ONLY="True"
            ;;
        *)
            echo "Unrecognized parameter: $1"
            exit 1
            ;;
    esac
    shift
done

# Copying the dist/ dir can cause permission issues, so just nuke it.
hatch clean 2> /dev/null || true

ARGS=""

if test "${PIP_INDEX_URL:-}" != ""; then
    # If PIP_INDEX_URL is set, then export that in to the container
    # so that `pip install` run in the container will fetch packages
    # from the correct repository.
    ARGS="${ARGS} -e PIP_INDEX_URL"
fi

if test "${USE_LDAP}" == "True"; then
    CONTAINER_HOSTNAME=ldap.environment.internal
    CONTAINER_IMAGE_TAG="openjd_ldap_test"
    CONTAINER_IMAGE_DIR="ldap_sudo_environment"
else
    CONTAINER_HOSTNAME=localuser.environment.internal
    CONTAINER_IMAGE_TAG="openjd_localuser_test"
    CONTAINER_IMAGE_DIR="localuser_sudo_environment"
fi
ARGS="${ARGS} -h ${CONTAINER_HOSTNAME}"

pip_index_arg=""
if test "${PIP_INDEX_URL:-}" != ""; then
    pip_index_arg="--build-arg PIP_INDEX_URL "
fi
docker build -t "${CONTAINER_IMAGE_TAG}" $pip_index_arg --build-arg "BUILDKIT_SANDBOX_HOSTNAME=${CONTAINER_HOSTNAME}" --file "testing_containers/${CONTAINER_IMAGE_DIR}/Dockerfile" .

if test "${BUILD_ONLY}" == "True"; then
    exit 0
fi

docker run --name test_openjd_sudo --rm ${ARGS} "${CONTAINER_IMAGE_TAG}:latest"
