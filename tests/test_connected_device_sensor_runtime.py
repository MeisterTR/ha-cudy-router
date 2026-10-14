"""Runtime tests for connected-client sensors, including clients that connect after setup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.module_loader import load_cudy_module


const = load_cudy_module("const")
sensor = load_cudy_module("sensor")

ENTRY_ID = "entry123"
LAPTOP = {"mac": "AA:BB:CC:DD:EE:01", "hostname": "laptop", "ip": "192.0.2.10", "up_speed": 1.5, "down_speed": 12.0}
TV_BOX = {"mac": "AA:BB:CC:DD:EE:02", "hostname": "tv-box", "ip": "192.0.2.11", "up_speed": 0.2, "down_speed": 8.4}
LAPTOP_ID = "aabbccddee01"
TV_BOX_ID = "aabbccddee02"


class _Registry:
    entities: dict = {}

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        return None

    def async_remove(self, entity_id: str) -> None:
        raise AssertionError(f"Unexpected removal: {entity_id}")


def _set_up(monkeypatch, devices, options=None, registry=None):
    """Run the sensor platform setup; return the coordinator, its update listeners and each batch of added entities."""
    listeners = []
    config_entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        data={"model": "WR3000"},
        options=options or {},
        title="WR3000",
        async_on_unload=lambda unload: None,
    )
    coordinator = SimpleNamespace(
        config_entry=config_entry,
        data={const.MODULE_DEVICES: {const.SECTION_DEVICE_LIST: [dict(device) for device in devices]}},
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    hass = SimpleNamespace(data={const.DOMAIN: {ENTRY_ID: coordinator}})

    monkeypatch.setattr(sensor, "async_get_entity_registry", lambda hass: registry or _Registry())
    monkeypatch.setattr(sensor, "known_client_devices", lambda hass, config_entry: {})
    monkeypatch.setattr(sensor, "async_cleanup_stale_client_entities", lambda *args: None)

    batches = []
    asyncio.run(sensor.async_setup_entry(hass, config_entry, lambda entities: batches.append(list(entities))))
    return coordinator, listeners, batches


def _client_unique_ids(entities, normalized_mac: str) -> list[str]:
    prefix = f"{ENTRY_ID}-device-{normalized_mac}-"
    return sorted(entity.unique_id for entity in entities if (entity.unique_id or "").startswith(prefix))


def _expected_unique_ids(normalized_mac: str) -> list[str]:
    return sorted(
        f"{ENTRY_ID}-device-{normalized_mac}-{description.key}" for description in sensor.CONNECTED_DEVICE_SENSOR_TYPES
    )


def _connect(coordinator, device) -> None:
    coordinator.data[const.MODULE_DEVICES][const.SECTION_DEVICE_LIST].append(dict(device))


def _refresh(listeners) -> None:
    for listener in listeners:
        listener()


def test_setup_creates_sensors_for_connected_clients(monkeypatch) -> None:
    """Every client connected at setup gets the full set of client sensors."""
    _coordinator, _listeners, batches = _set_up(monkeypatch, [LAPTOP])

    assert _client_unique_ids(batches[0], LAPTOP_ID) == _expected_unique_ids(LAPTOP_ID)


def test_client_connecting_after_setup_gets_its_sensors(monkeypatch) -> None:
    """A client that connects after setup gets the same sensors on the next refresh, once."""
    coordinator, listeners, batches = _set_up(monkeypatch, [LAPTOP])
    assert _client_unique_ids(batches[0], TV_BOX_ID) == []

    _connect(coordinator, TV_BOX)
    _refresh(listeners)

    assert len(batches) == 2
    assert _client_unique_ids(batches[1], TV_BOX_ID) == _expected_unique_ids(TV_BOX_ID)
    assert _client_unique_ids(batches[1], LAPTOP_ID) == []

    _refresh(listeners)
    assert len(batches) == 2


def test_client_speed_sensors_report_the_clients_current_rates(monkeypatch) -> None:
    """Each client gets upload and download rate sensors that follow the live client list."""
    coordinator, _listeners, batches = _set_up(monkeypatch, [LAPTOP])
    by_id = {entity.unique_id: entity for entity in batches[0]}

    upload = by_id[f"{ENTRY_ID}-device-{LAPTOP_ID}-up_speed"]
    download = by_id[f"{ENTRY_ID}-device-{LAPTOP_ID}-down_speed"]
    assert upload._attr_name == "Client upload"
    assert download._attr_name == "Client download"
    assert upload.entity_description.native_unit_of_measurement == "Mbit/s"
    assert (upload.native_value, download.native_value) == (1.5, 12.0)

    coordinator.data[const.MODULE_DEVICES][const.SECTION_DEVICE_LIST][0]["up_speed"] = 3.25
    assert upload.native_value == 3.25


def test_setup_keeps_existing_client_speed_sensors(monkeypatch) -> None:
    """The cleanup of retired router-level speed sensors must leave the per-client ones alone."""
    registry = _Registry()
    registry.entities = {
        "sensor.laptop_client_upload": SimpleNamespace(
            domain="sensor",
            platform=const.DOMAIN,
            entity_id="sensor.laptop_client_upload",
            unique_id=f"{ENTRY_ID}-device-{LAPTOP_ID}-up_speed",
        ),
    }

    _set_up(monkeypatch, [LAPTOP], registry=registry)

    assert "sensor.laptop_client_upload" in registry.entities


def test_clients_connecting_later_are_not_added_when_automatic_adding_is_off(monkeypatch) -> None:
    """With automatic adding off, only the user's own selection decides which clients get sensors."""
    coordinator, listeners, batches = _set_up(
        monkeypatch,
        [LAPTOP],
        options={const.OPTIONS_AUTO_ADD_CONNECTED_DEVICES: False},
    )

    _connect(coordinator, TV_BOX)
    _refresh(listeners)

    assert all(_client_unique_ids(batch, TV_BOX_ID) == [] for batch in batches)
