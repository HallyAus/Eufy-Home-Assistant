"""Regression for Home Assistant's config entry unload callback contract."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_snapshot_primer_unload_callback_returns_none_and_cancels_task():
    path = Path(__file__).resolve().parents[1] / "custom_components/eufy_nvr/__init__.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    setup = next(
        node for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )

    class Coordinator:
        def __init__(self, hass, entry):
            pass

        async def async_config_entry_first_refresh(self):
            pass

        async def async_prime_frames(self):
            pass

        async def async_prime_frames_forever(self):
            await asyncio.Event().wait()

    class Entry:
        data = {"username": "local", "password": "local"}
        unload = None

        def async_on_unload(self, callback):
            self.unload = callback

    class ConfigEntries:
        async def async_forward_entry_setups(self, entry, platforms):
            pass

    tasks = []

    def create_task(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    hass = SimpleNamespace(config_entries=ConfigEntries(), async_create_task=create_task)
    namespace = {
        "asyncio": asyncio,
        "HomeAssistant": object,
        "EufyNvrConfigEntry": object,
        "CONF_USERNAME": "username",
        "CONF_PASSWORD": "password",
        "DOMAIN": "eufy_nvr",
        "PLATFORMS": [],
        "FRAME_SETUP_PRIME_TIMEOUT": 1.0,
        "validate_credentials": lambda username, password: None,
        "EufyNvrCoordinator": Coordinator,
        "ConfigEntryAuthFailed": RuntimeError,
    }
    exec(compile(ast.Module(body=[setup], type_ignores=[]), str(path), "exec"), namespace)

    entry = Entry()
    assert await namespace["async_setup_entry"](hass, entry) is True
    await asyncio.sleep(0)
    assert entry.unload() is None
    await asyncio.sleep(0)
    assert tasks[0].cancelled()
