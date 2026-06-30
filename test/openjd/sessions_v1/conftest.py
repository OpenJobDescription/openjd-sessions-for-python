# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import sys
import pytest


def pytest_collection_modifyitems(config, items):
    mark_expr = config.getoption("markexpr", False)
    if not mark_expr:
        config.option.markexpr = "not requires_cap_kill"
    else:
        config.option.markexpr = mark_expr


@pytest.fixture(scope="function")
def session_id() -> str:
    return "some Id"


@pytest.fixture(scope="function")
def python_exe() -> str:
    return sys.executable
