"""Neue Plattform: text-Entity für das WISP-WLAN-Passwort.

Neue Datei für `custom_components/cudy_router/text.py`. Home Assistant hat
keine eingebaute "Passwort eingeben"-Karte für Buttons/Selects, aber die
native `text`-Domain mit `mode="password"` erledigt das sauber (maskiertes
Eingabefeld im Dashboard).

Wichtig: Platform.TEXT muss in `__init__.py` zur PLATFORMS-Liste ergänzt
werden - siehe README.md in diesem Ordner für die genaue Änderung.
"""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODULE_WISP
from .coordinator import CudyRouterDataUpdateCoordinator
from .device_info import build_router_device_info
from .features import module_available

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Cudy Router text entities."""
    coordinator: CudyRouterDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    device_model = config_entry.data.get(CONF_MODEL, "default")

    entities = []
    if module_available(device_model, MODULE_WISP, coordinator.data):
        entities.append(CudyWispPasswordText(coordinator))

    async_add_entities(entities)


class CudyWispPasswordText(
    CoordinatorEntity[CudyRouterDataUpdateCoordinator], TextEntity
):
    """Passwort-Eingabefeld für die WISP-Verbindung.

    Wert liegt (nur im Speicher, nicht persistiert) in
    `coordinator.wisp_password` und wird von `button.*_wisp_verbinden`
    gelesen. Leer lassen für offene Netze.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi-lock"
    _attr_name = "WISP: Passwort"
    _attr_mode = TextMode.PASSWORD
    _attr_native_max = 63

    def __init__(self, coordinator: CudyRouterDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}-wisp-password"
        self._attr_device_info = build_router_device_info(coordinator)

    @property
    def native_value(self) -> str:
        return getattr(self.coordinator, "wisp_password", "") or ""

    async def async_set_value(self, value: str) -> None:
        self.coordinator.wisp_password = value
        self.async_write_ha_state()
