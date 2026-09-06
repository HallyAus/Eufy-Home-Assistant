"""Read-only bridge diagnostics. Counts do not imply working camera video."""
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.helpers.entity import EntityCategory

from .entity import EufyNvrEntity
from .go2rtc_api import stream_summary

PARALLEL_UPDATES = 0
DESCRIPTIONS = (
    SensorEntityDescription(key="configured_streams", translation_key="configured_streams",
                            icon="mdi:cctv", entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="active_consumers", translation_key="active_consumers",
                            icon="mdi:account-eye", entity_category=EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(EufyNvrSensor(entry.runtime_data, entry.entry_id, description)
                       for description in DESCRIPTIONS)


class EufyNvrSensor(EufyNvrEntity, SensorEntity):
    """A count derived from the existing go2rtc poll."""
    def __init__(self, coordinator, entry_id, description) -> None:
        super().__init__(coordinator, entry_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int:
        streams = self.coordinator.data or {}
        if self.entity_description.key == "configured_streams":
            return len(streams)
        return sum(stream_summary(info)["consumers"] for info in streams.values())
