"""Sensor entities for Alexa Shopping List Sync."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AlexaShoppingCoordinator

SENSOR_DESCRIPTIONS = [
    SensorEntityDescription(
        key="last_success",
        translation_key="last_success",
        icon="mdi:clock-check-outline",
    ),
    SensorEntityDescription(
        key="last_error",
        translation_key="last_error",
        icon="mdi:alert-circle-outline",
    ),
    SensorEntityDescription(
        key="pending_operations",
        translation_key="pending_operations",
        icon="mdi:sync",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="ops",
    ),
    SensorEntityDescription(
        key="alexa_items",
        translation_key="alexa_items",
        icon="mdi:format-list-bulleted",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="items",
    ),
    SensorEntityDescription(
        key="ha_items",
        translation_key="ha_items",
        icon="mdi:format-list-checks",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="items",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities."""
    coordinator: AlexaShoppingCoordinator = entry.runtime_data

    entities = [
        AlexaShoppingSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class AlexaShoppingSensor(
    CoordinatorEntity[AlexaShoppingCoordinator], SensorEntity
):
    """Sensor entity for Alexa Shopping List Sync."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AlexaShoppingCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Alexa Shopping Sync ({entry.title})",
            "manufacturer": "Amazon",
            "model": "Alexa Shopping List",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | int | None:
        """Return the state of the sensor."""
        key = self.entity_description.key

        if key == "last_success":
            ts = self.coordinator.last_success
            if ts:
                try:
                    dt = datetime.fromtimestamp(float(ts))
                    return dt.isoformat(timespec="seconds")
                except (ValueError, OSError):
                    return ts
            return "Never"

        if key == "last_error":
            return self.coordinator.last_error or "None"

        if key == "pending_operations":
            return self.coordinator.pending_operations_count

        if key == "alexa_items":
            return self.coordinator.alexa_item_count

        if key == "ha_items":
            return self.coordinator.ha_item_count

        return None
