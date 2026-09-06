"""Exercise the actual HA flow, coordinator, entities and lifecycle APIs."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
pytest.importorskip("homeassistant")
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_nvr import async_setup_entry, async_unload_entry
from custom_components.eufy_nvr import camera, diagnostics
from custom_components.eufy_nvr.binary_sensor import EufyNvrConnectivity
from custom_components.eufy_nvr.button import EufyNvrRefresh
from custom_components.eufy_nvr.config_flow import _validate_go2rtc, WrongInstance, NoStreams
from custom_components.eufy_nvr.coordinator import EufyNvrCoordinator
from custom_components.eufy_nvr.go2rtc_api import Go2RtcAuthenticationError, Go2RtcConnectionError, Go2RtcPayloadError
from custom_components.eufy_nvr.sensor import EufyNvrSensor, DESCRIPTIONS

DOMAIN = "eufy_nvr"
DATA = {"host": "bridge.local", "api_port": 1985, "rtsp_port": 8556}
CLIENT = "custom_components.eufy_nvr.go2rtc_api.Go2RtcClient.async_get_streams"

@pytest.fixture
def entry(hass):
    item = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="bridge.local:1985", options={})
    item.add_to_hass(hass)
    return item

@pytest.fixture
def prevent_setup():
    with patch("homeassistant.config_entries.ConfigEntry.async_setup", return_value=True):
        yield

@pytest.mark.asyncio
async def test_flow_creates_normalized_endpoint(hass, prevent_setup):
    with patch(CLIENT, return_value={"eufy_front": {}}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}, data={**DATA, "host": "BRIDGE.Local."})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == DATA
    assert result["result"].unique_id == "bridge.local:1985"
    await hass.async_block_till_done()

@pytest.mark.asyncio
@pytest.mark.parametrize("error,key", [(Go2RtcConnectionError(), "cannot_connect"), (Go2RtcAuthenticationError(), "auth_required"), (Go2RtcPayloadError(), "invalid_response")])
async def test_flow_actionable_errors(hass, error, key):
    with patch(CLIENT, side_effect=error):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}, data=DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("api_port", 0), ("api_port", 65536), ("rtsp_port", -1), ("host", "invalid/host")])
async def test_invalid_input_never_probes_network(hass, field, value):
    with patch(CLIENT) as request:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}, data={**DATA, field: value})
    assert result["errors"] == {"base": "invalid_endpoint"}
    request.assert_not_called()

@pytest.mark.asyncio
async def test_duplicate_uses_data_even_with_stale_unique_id(hass, entry):
    hass.config_entries.async_update_entry(entry, unique_id="old-host:1985")
    with patch(CLIENT, return_value={"eufy_front": {}}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}, data=DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

@pytest.mark.asyncio
async def test_reconfigure_updates_identity_without_replacing_entry(hass, entry):
    old_id = entry.entry_id
    with patch(CLIENT, return_value={"eufy_front": {}}), patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}, data={**DATA, "host": "new.local"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "new.local"
    assert entry.unique_id == "new.local:1985"
    assert entry.entry_id == old_id

@pytest.mark.asyncio
async def test_reconfigure_cannot_steal_another_entry(hass, entry):
    other = MockConfigEntry(domain=DOMAIN, data={**DATA, "host": "other.local"}, unique_id="other.local:1985")
    other.add_to_hass(hass)
    with patch(CLIENT, return_value={"eufy_front": {}}):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}, data=dict(other.data))
    assert result["errors"] == {"base": "already_configured"}
    assert entry.data == DATA

@pytest.mark.asyncio
async def test_empty_and_wrong_instance_are_distinct(hass):
    with patch(CLIENT, return_value={}):
        with pytest.raises(NoStreams):
            await _validate_go2rtc(hass, "bridge.local", 1985)
    with patch("custom_components.eufy_nvr.config_flow.Go2RtcClient") as client:
        client.return_value.async_get_streams = AsyncMock(return_value={})
        client.return_value.total_stream_count = 2
        with pytest.raises(WrongInstance):
            await _validate_go2rtc(hass, "bridge.local", 1985)

@pytest.mark.asyncio
async def test_options_saved_separately(hass, entry):
    initial = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(initial["flow_id"], user_input={"scan_interval": 60, "snapshot_interval": 45})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {"scan_interval": 60, "snapshot_interval": 45}
    assert entry.data == DATA

@pytest.mark.asyncio
async def test_coordinator_failure_recovery_and_options(hass, entry):
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 60})
    coordinator = EufyNvrCoordinator(hass, entry)
    assert coordinator.update_interval.total_seconds() == 60
    with patch(CLIENT, side_effect=Go2RtcConnectionError("endpoint unavailable")):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert coordinator.consecutive_failures == 1
    with patch(CLIENT, return_value={"eufy_front": {}}):
        assert await coordinator._async_update_data() == {"eufy_front": {}}
    assert coordinator.consecutive_failures == 0
    assert coordinator.successful_polls == 1
    await coordinator.async_shutdown()

@pytest.mark.asyncio
async def test_dynamic_cameras_unavailable_and_snapshot_cache(hass, entry):
    coordinator = EufyNvrCoordinator(hass, entry)
    entry.runtime_data = coordinator
    coordinator.async_set_updated_data({"eufy_front": {}})
    entities = []
    await camera.async_setup_entry(hass, entry, lambda items: entities.extend(items))
    assert len(entities) == 1
    front = entities[0]
    front.hass = hass
    assert front.unique_id == f"eufy_nvr_{entry.entry_id}_eufy_front"
    with patch("custom_components.eufy_nvr.camera.async_get_image", return_value=b"image") as fetch:
        assert await front.async_camera_image(640, 360) == b"image"
        assert await front.async_camera_image(640, 360) == b"image"
        fetch.assert_awaited_once()
    coordinator.async_set_updated_data({"eufy_front": {}, "eufy_back": {}})
    assert len(entities) == 2
    coordinator.async_set_updated_data({"eufy_back": {}})
    assert not front.available
    assert await front.async_camera_image() is None
    assert front._snapshots._image is None
    coordinator.async_set_updated_data({"eufy_front": {}, "eufy_back": {}})
    assert front.available
    assert len(entities) == 2
    await coordinator.async_shutdown()

@pytest.mark.asyncio
async def test_bridge_entities_and_redacted_diagnostics(hass, entry):
    coordinator = EufyNvrCoordinator(hass, entry)
    entry.runtime_data = coordinator
    coordinator.async_set_updated_data({"eufy_private_room": {"producers": [{"url": "rtsp://user:SECRET@private-host"}], "consumers": [{}, {}]}})
    counts = [EufyNvrSensor(coordinator, entry.entry_id, description) for description in DESCRIPTIONS]
    assert [entity.native_value for entity in counts] == [1, 2]
    connectivity = EufyNvrConnectivity(coordinator, entry.entry_id, "bridge_connection")
    assert connectivity.is_on
    coordinator.last_update_success = False
    assert connectivity.available and not connectivity.is_on
    coordinator.last_exception = RuntimeError("SECRET private-host")
    result = json.dumps(await diagnostics.async_get_config_entry_diagnostics(hass, entry))
    for value in ("SECRET", "private-host", "private_room", "bridge.local"):
        assert value not in result
    button = EufyNvrRefresh(coordinator, entry.entry_id, "refresh_streams")
    coordinator.last_update_success = True
    with patch.object(coordinator, "async_request_refresh", return_value=None) as refresh:
        await button.async_press()
        refresh.assert_awaited_once()
    await coordinator.async_shutdown()

@pytest.mark.asyncio
async def test_integration_lifecycle_registers_all_platforms(hass, entry):
    with patch(CLIENT, return_value={"eufy_front": {}}), patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None) as forward:
        assert await async_setup_entry(hass, entry)
        assert {str(value) for value in forward.call_args.args[1]} == {"camera", "sensor", "binary_sensor", "button"}
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, entry)
    await entry.runtime_data.async_shutdown()
