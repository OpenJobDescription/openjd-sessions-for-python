#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

set -eu

# Run this from the root of the repository
if ! test -d scripts
then
    echo "Must run from the root of the repository"
    exit 1
fi

while [[ "${1:-}" != "" ]]; do
    case $1 in
        -h|--help)
            echo "Usage: $0 [--build]"
            exit 1
            ;;
        --build)
            docker build testing_containers/codebuild_proxy -t openjd_codebuild_proxy
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

if test "${PIP_INDEX_URL:-}" != ""; then
    docker run --rm -v $(pwd):/code:ro -e PIP_INDEX_URL="${PIP_INDEX_URL}" openjd_codebuild_proxy:latest 
else
    docker run --rm -v $(pwd):/code:ro  openjd_codebuild_proxy:latest 
fi