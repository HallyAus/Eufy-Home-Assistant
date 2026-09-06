"""Tests backed by Home Assistant itself; no eufy account or NVR required."""
import pytest

@pytest.fixture(autouse=True)
def custom_integration(enable_custom_integrations):
    yield
