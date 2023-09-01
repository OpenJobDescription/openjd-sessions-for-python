#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

set -eux

#DIR=/home/codebuild-test
DIR=/root

mkdir -p "${DIR}"/code/
cp -r /code/* "${DIR}"/code/
cp -r /code/.git "${DIR}"/code/

cd "${DIR}"/code
find . -type d -name '__pycache__' -print0 | xargs -0 rm -rf
ls -la
"${DIR}"/code/pipeline/build.sh
