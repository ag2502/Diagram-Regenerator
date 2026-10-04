import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_package_logger():
    """The CLI points the package logger at stderr; undo that between tests."""
    yield
    logger = logging.getLogger("diagram_regenerator")
    logger.handlers.clear()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
