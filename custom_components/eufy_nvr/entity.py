"""Shared device identity for the bridge and its camera entities."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_NAME, DOMAIN, MANUFACTURER, MODEL
from .coordinator import EufyNvrCoordinator
from .go2rtc_api import api_url


def bridge_device_info(coordinator: EufyNvrCoordinator, entry_id: str) -> DeviceInfo:
    """Keep the existing device identity across reconfiguration."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)}, name=DEVICE_NAME,
        manufacturer=MANUFACTURER, model=MODEL,
        configuration_url=api_url(coordinator.host, coordinator.api_port).removesuffix("/api/streams"),
    )


class EufyNvrEntity(CoordinatorEntity[EufyNvrCoordinator]):
    """A diagnostic entity sharing the camera coordinator, without extra polling."""
    _attr_has_entity_name = True

    def __init__(self, coordinator: EufyNvrCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = bridge_device_info(coordinator, entry_id)
