"""tern.log: one JSON object per line, with extra fields."""

import io
import json
import logging
from collections.abc import Iterator

import pytest

from tern import log


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    # log.setup replaces the root logger's handlers; leave the root as we found it, so later
    # tests (caplog, other modules' logging) are not routed into this test's buffer.
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


def test_json_lines_with_extra_fields() -> None:
    buf = io.StringIO()
    log.setup(buf, logging.INFO)
    logging.getLogger("tern.test").info("peer rejected", extra={"reason": "parent", "n": 2})
    logging.getLogger("tern.test").debug("hidden")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["level"] == "INFO" and rec["logger"] == "tern.test"
    assert rec["msg"] == "peer rejected" and rec["reason"] == "parent" and rec["n"] == 2
    assert rec["ts"].endswith("+00:00")


def test_setup_leaves_no_trace_in_later_tests() -> None:
    # Runs after the test above (file order): the fixture restored the root logger, so no
    # tern JSON handler is left behind (pytest's own capture handlers are not ours to judge).
    root = logging.getLogger()
    assert not any(isinstance(h.formatter, log.JsonFormatter) for h in root.handlers)
