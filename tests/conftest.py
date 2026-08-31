from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from main import app


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: test requires live infrastructure")
    config.addinivalue_line("markers", "e2e: end-to-end test")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
