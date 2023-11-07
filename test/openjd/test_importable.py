# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.


def test_openjd_importable():
    import openjd  # noqa: F401


def test_openjd_session_importable():
    import openjd.sessions  # noqa: F401


def test_version_importable():
    from openjd.sessions import version  # noqa: F401
