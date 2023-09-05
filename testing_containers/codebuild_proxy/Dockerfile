# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

FROM public.ecr.aws/codebuild/amazonlinux2-x86_64-standard:4.0

WORKDIR /root

COPY run_tests.sh .

USER root

ENTRYPOINT [ "" ]
CMD ["/bin/sh", "-c", "./run_tests.sh"]
