"""Bridge API connectivity, explicitly distinct from camera video health."""
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from .entity import EufyNvrEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([EufyNvrConnectivity(entry.runtime_data, entry.entry_id, "bridge_connection")])


class EufyNvrConnectivity(EufyNvrEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Remain visible as disconnected during an API outage."""
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
