# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""The `serial_process` mechanism must keep working, silently-failing being its
whole risk.

`@pytest.mark.xdist_group` is only honoured under `--dist=loadgroup`. Under the
default `--dist=load` it is accepted, ignored, and reported nowhere -- so the
process-heavy tests would quietly go back to competing with eleven siblings and
the cancel/terminate family would start failing together again on loaded hosts,
looking exactly like a product regression.

These tests are cheap and exist so that a change to `addopts` fails here, next to
an explanation, rather than as fourteen mystery failures somewhere else.
"""

from __future__ import annotations

import pytest

from .conftest import SERIAL_PROCESS_GROUP, serial_process


def test_loadgroup_distribution_is_configured(pytestconfig: pytest.Config) -> None:
    """Pins the `--dist=loadgroup` setting the marker depends on.

    Read from `addopts` rather than from `getoption("dist")`: inside an xdist
    worker the resolved `dist` option is `"no"`, because a worker runs its own
    share serially and only the controller distributes. Asserting on the resolved
    option therefore fails on the workers and passes nowhere useful -- which is
    exactly what the first version of this test did.
    """
    # GIVEN / WHEN
    addopts = " ".join(pytestconfig.getini("addopts"))

    # THEN
    assert "--dist=loadgroup" in addopts, (
        "xdist_group markers are silently ignored unless --dist=loadgroup; the "
        "serial_process tests would go back to running in parallel with each other"
    )


def test_serial_process_is_an_xdist_group_marker() -> None:
    """The mark must be the one xdist looks for, with the expected group name.

    A rename or a typo would leave a decorator that reads as if it does something
    and does nothing at all.
    """
    # GIVEN / WHEN
    mark = serial_process.mark

    # THEN
    assert mark.name == "xdist_group"
    assert mark.args == (SERIAL_PROCESS_GROUP,)


@serial_process
class TestMarkerReachesTheTest:
    def test_the_marker_is_visible_on_a_decorated_test(
        self, request: pytest.FixtureRequest
    ) -> None:
        """A class-level mark must actually reach the test item.

        This is what the quiesce fixture in conftest keys off, so if the mark stops
        propagating the cleanup silently stops running too.
        """
        # GIVEN / WHEN
        marker = request.node.get_closest_marker("xdist_group")

        # THEN
        assert marker is not None
        assert SERIAL_PROCESS_GROUP in marker.args
