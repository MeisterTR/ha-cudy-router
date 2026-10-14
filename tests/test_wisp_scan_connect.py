"""Tests for WISP scan and connect support."""

from __future__ import annotations

import asyncio
import urllib.parse
from types import SimpleNamespace

from tests.module_loader import load_cudy_module


wisp = load_cudy_module("wisp_extra")
text = load_cudy_module("text")
select = load_cudy_module("select")
button = load_cudy_module("button")


def _row(no: int, bssid: str, ssid: str, channel: str, signal: str, icon: str) -> str:
    return (
        "<tr>"
        f"<td>{no}</td><td>2.4G2.4G</td><td>{bssid}</td><td>{ssid}</td><td>{ssid}</td>"
        f"<td>{channel}</td><td>{signal}</td><td>-</td>"
        f'<td><i class="icon {icon}"></i><a>Join</a></td>'
        "</tr>"
    )


SCAN_HTML = (
    '<table class="table-striped"><tr><th>No</th></tr>'
    + _row(1, "AA:BB:CC:00:00:01", "WeakNet", "6", "-80 dBm", "icon-lock")
    + _row(2, "AA:BB:CC:00:00:02", "OpenNet", "1", "-40 dBm", "icon-none")
    + _row(3, "AA:BB:CC:00:00:02", "OpenNet", "1", "-40 dBm", "icon-none")
    + "</table>"
)


def test_scan_parser_sorts_by_signal_dedupes_and_flags_encryption() -> None:
    networks = wisp._parse_wisp_scan_networks(SCAN_HTML)

    assert [n["ssid"] for n in networks] == ["OpenNet", "WeakNet"]
    assert networks[0]["signal_dbm"] == -40
    assert networks[0]["encrypted"] is False
    assert networks[1]["encrypted"] is True
    assert networks[0]["band"] == "2.4G"


def test_scan_parser_without_table_returns_empty_list() -> None:
    assert wisp._parse_wisp_scan_networks("<html></html>") == []


class _FakeApi:
    base_url = "http://192.168.10.1"

    def __init__(self, token_html: str = '<input name="token" value="tok" />') -> None:
        self.token_html = token_html
        self.calls: list[tuple] = []

    def get(self, path, *args):
        self.calls.append(("get", path))
        return self.token_html

    def _luci_post(self, path, *, timeout=None, headers=None, data=None):
        self.calls.append(("post", path, data))
        return SimpleNamespace(status_code=200, text="")

    def _luci_get(self, path, *, timeout=None, headers=None):
        self.calls.append(("luci_get", path))
        return SimpleNamespace(status_code=200, text=SCAN_HTML)

    def _submit_form(self, fetch_path, overrides, *, referer=None):
        self.calls.append(("submit", fetch_path, overrides))
        return 200, "Configuration applied."


def test_scan_wisp_posts_doscan_with_token_then_reads_the_table() -> None:
    api = _FakeApi()

    status, networks = wisp.scan_wisp(api)

    assert status == 200
    assert [n["ssid"] for n in networks] == ["OpenNet", "WeakNet"]
    kinds = [call[0] for call in api.calls]
    assert kinds == ["get", "post", "luci_get"]
    assert api.calls[1][1] == "admin/network/wireless/wds/doscan"
    assert urllib.parse.parse_qs(api.calls[1][2])["token"] == ["tok"]


def test_scan_wisp_without_token_fails_without_posting() -> None:
    api = _FakeApi(token_html="")

    status, result = wisp.scan_wisp(api)

    assert status == 0
    assert isinstance(result, str)
    assert [call[0] for call in api.calls] == ["get"]


def test_join_wisp_overrides_only_the_password_by_default() -> None:
    api = _FakeApi()

    status, _message = wisp.join_wisp(api, "My Net", "AA:BB:CC:00:00:02", "secret")

    assert status == 200
    _kind, path, overrides = api.calls[0]
    assert "ssid=My%20Net" in path
    assert "bssid=AA%3ABB%3ACC%3A00%3A00%3A02" in path
    assert overrides == {"cbid.wireless.wdstmp.key": "secret"}


def test_join_wisp_applies_protocol_and_hostname_when_given() -> None:
    api = _FakeApi()

    wisp.join_wisp(api, "Net", "AA:BB:CC:00:00:02", "", hostname="caravan", proto="dhcp")

    assert api.calls[0][2] == {
        "cbid.wireless.wdstmp.key": "",
        "cbid.network.wisp.proto": "dhcp",
        "cbid.network.wisp.hostname": "caravan",
    }


def test_password_entity_never_exposes_the_password_in_its_state() -> None:
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry123"),
        data={},
        api=SimpleNamespace(),
    )
    entity = text.CudyWispPasswordText.__new__(text.CudyWispPasswordText)
    entity.coordinator = coordinator
    entity.async_write_ha_state = lambda: None

    assert entity.native_value == ""

    asyncio.run(entity.async_set_value("hunter2"))

    assert coordinator.wisp_password == "hunter2"
    assert entity.native_value == text.PASSWORD_MASK
    assert "hunter2" not in entity.native_value


def test_network_select_lists_unique_ssids_and_ignores_the_placeholder() -> None:
    coordinator = SimpleNamespace(
        wisp_networks=[
            {"ssid": "A", "bssid": "1"},
            {"ssid": "A", "bssid": "2"},
            {"ssid": "B", "bssid": "3"},
        ],
    )
    assert select._unique_ssid_options(coordinator) == ["A", "B"]
    assert select._unique_ssid_options(SimpleNamespace(wisp_networks=[])) == [select.NO_NETWORKS_LABEL]

    entity = select.CudyWispNetworkSelect.__new__(select.CudyWispNetworkSelect)
    entity.coordinator = SimpleNamespace(wisp_networks=[], wisp_selected_ssid=None)
    entity.async_write_ha_state = lambda: None
    asyncio.run(entity.async_select_option(select.NO_NETWORKS_LABEL))
    assert entity.coordinator.wisp_selected_ssid is None


def test_connect_button_refuses_without_a_scanned_selection() -> None:
    calls: list = []
    entity = button.CudyWispConnectButton.__new__(button.CudyWispConnectButton)
    entity.coordinator = SimpleNamespace(wisp_networks=[{"ssid": "A", "bssid": "1"}], wisp_selected_ssid="Other")
    entity.hass = SimpleNamespace(async_add_executor_job=lambda *args: calls.append(args))

    asyncio.run(entity.async_press())

    assert calls == []
