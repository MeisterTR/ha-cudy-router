"""Support for Cudy Router Button Platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODULE_MESH, MODULE_WISP
from .coordinator import CudyRouterDataUpdateCoordinator
from .device_info import (
    async_cleanup_stale_mesh_entities,
    build_mesh_device_info,
    build_router_device_info,
    mesh_display_name,
    router_display_name,
)
from .features import module_available
from .wisp_extra import join_wisp, scan_wisp

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Cudy Router buttons."""
    coordinator: CudyRouterDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    router_name = router_display_name(config_entry, coordinator.data)

    entities: list[ButtonEntity] = []

    # Add main router reboot button
    entities.append(
        CudyRouterRebootButton(
            coordinator,
            router_name,
        )
    )

    # WISP scan and connect buttons
    device_model = config_entry.data.get(CONF_MODEL, "default")
    if module_available(device_model, MODULE_WISP, coordinator.data):
        entities.append(CudyWispScanButton(coordinator, router_name))
        entities.append(CudyWispConnectButton(coordinator, router_name))

    # Add mesh device reboot buttons
    if coordinator.data:
        mesh_data = coordinator.data.get(MODULE_MESH, {})
        mesh_devices = mesh_data.get("mesh_devices", {})
        async_cleanup_stale_mesh_entities(
            hass,
            config_entry,
            "button",
            set(mesh_devices),
        )

        for mesh_mac, mesh_device in mesh_devices.items():
            entities.append(
                CudyMeshRebootButton(
                    coordinator,
                    router_name,
                    mesh_mac,
                    mesh_device,
                )
            )

    async_add_entities(entities)


class CudyRouterRebootButton(
    CoordinatorEntity[CudyRouterDataUpdateCoordinator], ButtonEntity
):
    """Button to reboot the main Cudy router."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"
    _attr_name = "Reboot"

    def __init__(
        self,
        coordinator: CudyRouterDataUpdateCoordinator,
        router_name: str | None,
    ) -> None:
        """Initialize the reboot button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}-reboot"
        self._attr_device_info = build_router_device_info(coordinator)

    async def async_press(self) -> None:
        """Handle the button press - reboot the router."""
        _LOGGER.info("Rebooting main Cudy router")
        result = await self.hass.async_add_executor_job(
            self.coordinator.api.reboot_router
        )
        if result[0] not in (200, 302):
            _LOGGER.error("Failed to reboot router: %s", result[1])


class CudyMeshRebootButton(
    CoordinatorEntity[CudyRouterDataUpdateCoordinator], ButtonEntity
):
    """Button to reboot a mesh device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: CudyRouterDataUpdateCoordinator,
        router_name: str | None,
        mesh_mac: str,
        mesh_device: dict[str, Any],
    ) -> None:
        """Initialize the mesh reboot button."""
        super().__init__(coordinator)
        self._mesh_mac = mesh_mac
        self._mesh_name = mesh_display_name(mesh_device.get("name"), mesh_mac)
        self._attr_name = "Reboot"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}-mesh-{mesh_mac}-reboot"
        )
        self._attr_device_info = build_mesh_device_info(
            coordinator,
            mesh_mac,
            mesh_device,
        )

    async def async_press(self) -> None:
        """Handle the button press - reboot the mesh device."""
        _LOGGER.info("Rebooting mesh device: %s (%s)", self._mesh_name, self._mesh_mac)
        result = await self.hass.async_add_executor_job(
            self.coordinator.api.reboot_mesh_device, self._mesh_mac
        )
        if result[0] not in (200, 302):
            _LOGGER.error(
                "Failed to reboot mesh device %s: %s", self._mesh_name, result[1]
            )


class CudyWispScanButton(
    CoordinatorEntity[CudyRouterDataUpdateCoordinator], ButtonEntity
):
    """Button that runs a WISP Wi-Fi scan and stores the results.

    The networks found are kept in `coordinator.wisp_networks` (a list of dicts)
    and pushed to `select.*_wisp_network` via `async_update_listeners()`, without
    touching the regular polling cycle that updates all other sensors.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi-refresh"
    _attr_name = "WISP scan"

    def __init__(
        self,
        coordinator: CudyRouterDataUpdateCoordinator,
        router_name: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}-wisp-scan"
        self._attr_device_info = build_router_device_info(coordinator)

    async def async_press(self) -> None:
        """Start the scan (takes about 15 s on the router)."""
        _LOGGER.info("Starting WISP WiFi scan")
        status, result = await self.hass.async_add_executor_job(
            scan_wisp, self.coordinator.api
        )
        if status not in (200, 302) or isinstance(result, str):
            _LOGGER.error("WISP scan failed: %s", result)
            self.coordinator.wisp_networks = []
        else:
            self.coordinator.wisp_networks = result
            _LOGGER.info("WISP scan found %d network(s)", len(result))
        self.coordinator.async_update_listeners()


class CudyWispConnectButton(
    CoordinatorEntity[CudyRouterDataUpdateCoordinator], ButtonEntity
):
    """Button that joins the WISP uplink to the network chosen in
    `select.*_wisp_network`, using the password from `text.*_wisp_password`.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi-plus"
    _attr_name = "WISP connect"

    def __init__(
        self,
        coordinator: CudyRouterDataUpdateCoordinator,
        router_name: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}-wisp-connect"
        self._attr_device_info = build_router_device_info(coordinator)

    async def async_press(self) -> None:
        networks = getattr(self.coordinator, "wisp_networks", [])
        selected_ssid = getattr(self.coordinator, "wisp_selected_ssid", None)
        password = getattr(self.coordinator, "wisp_password", "")

        if not selected_ssid:
            _LOGGER.error(
                "WISP connect: no network selected (select.*_wisp_network)"
            )
            return

        match = next((n for n in networks if n["ssid"] == selected_ssid), None)
        if not match:
            _LOGGER.error(
                "WISP connect: '%s' is not in the last scan result - please scan again",
                selected_ssid,
            )
            return

        _LOGGER.info("Connecting WISP uplink to '%s'", selected_ssid)
        status, message = await self.hass.async_add_executor_job(
            join_wisp,
            self.coordinator.api,
            match["ssid"],
            match["bssid"],
            password,
        )
        if status not in (200, 302):
            _LOGGER.error("WISP connect failed: %s", message)
        else:
            _LOGGER.info("WISP connect result: %s", message)
            await self.coordinator.async_request_refresh()
