"""Refresh published streams without restarting cameras or logging into eufy."""
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from .entity import EufyNvrEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([EufyNvrRefresh(entry.runtime_data, entry.entry_id, "refresh_streams")])


class EufyNvrRefresh(EufyNvrEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()
        if not self.coordinator.last_update_success:
            raise HomeAssistantError("The Eufy bridge API could not be refreshed")
