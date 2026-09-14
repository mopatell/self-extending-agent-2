import pytest

from sea.db import DB


@pytest.fixture
def db() -> DB:
    d = DB(":memory:")
    yield d
    d.close()
