"""WISP scan and connect support for the Cudy Router integration.

Adds scanning for Wi-Fi networks and joining one as the WISP uplink, which
``CudyRouter.set_wisp_setting()`` (enable/disable only) does not cover.

Reverse-engineered from the router's own web UI (firmware 2.5.12). It reuses
the session, token and retry handling of ``CudyRouter`` (``get``, ``_luci_get``,
``_luci_post``, ``_submit_form``) like the existing ``set_*_setting()`` methods.

Request flow, as sent by the LuCI web UI:
1. POST admin/network/wireless/wds/doscan: starts the scan (blocks for ~15 s).
2. GET  admin/network/wireless/wds/scan?embedded=&nomodal=&mode=wisp:
        HTML table of the networks found.
3. GET  admin/network/wireless/wds/join?embedded=&nomodal=&mode=wisp&ssid=..&bssid=..&iface=wlan00:
        pre-filled form (token, cbid.wireless.wdstmp.ssid/key/iface,
        cbid.network.wisp.proto/hostname).
4. POST the same path with the password filled in: joins the network. The
        apply step is handled by ``_submit_form()``.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from .bs4_compat import BeautifulSoup

if TYPE_CHECKING:
    from .router import CudyRouter

_LOGGER = logging.getLogger(__name__)

DEFAULT_POST_TIMEOUT = 30
DEFAULT_GET_TIMEOUT = 30
WISP_IFACE = "wlan00"


def _extract_hidden(html: str, name: str) -> str:
    """Extract the value of a hidden input field."""
    match = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return match.group(1) if match else ""


def _dedupe_cell_text(text: str) -> str:
    """Collapse the doubled text of a scan table cell.

    Each cell holds a visible and a hidden screen-reader copy of its text, which
    ``get_text()`` glues together without a separator ("Free WiFiFree WiFi").
    An exact doubling (even length, identical halves) is cut back to one copy.
    """
    n = len(text)
    if n > 0 and n % 2 == 0:
        half = n // 2
        if text[:half] == text[half:]:
            return text[:half]
    return text


def _parse_wisp_scan_networks(html: str) -> list[dict[str, Any]]:
    """Parse the scan result table of the WISP page.

    Each data row has 9 <td>: [No, Band, BSSID, long SSID, short SSID, channel,
    signal, -, connect button]. An <i class="icon icon-lock"> marks encrypted
    networks and "icon-none" open ones.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-striped")
    if table is None:
        _LOGGER.debug("WISP scan: no result table found in the HTML")
        return []

    networks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 9:
            continue  # header or spacer row

        band = _dedupe_cell_text(tds[1].get_text(strip=True))
        bssid = _dedupe_cell_text(tds[2].get_text(strip=True))
        ssid = _dedupe_cell_text(tds[3].get_text(strip=True))
        channel = _dedupe_cell_text(tds[5].get_text(strip=True))
        signal_text = _dedupe_cell_text(tds[6].get_text(strip=True))
        if not bssid or not ssid:
            continue

        signal_match = re.search(r"-?\d+", signal_text)
        signal = int(signal_match.group()) if signal_match else None

        icon = tr.select_one("i.icon")
        icon_classes = icon.get("class", []) if icon else []
        encrypted = bool(icon) and "icon-none" not in icon_classes

        key = (bssid, ssid)
        if key in seen:
            continue
        seen.add(key)

        networks.append(
            {
                "ssid": ssid,
                "bssid": bssid,
                "band": band,
                "channel": channel,
                "signal_dbm": signal,
                "encrypted": encrypted,
            }
        )

    networks.sort(key=lambda n: (n["signal_dbm"] is None, -(n["signal_dbm"] or -999)))
    return networks


def scan_wisp(api: "CudyRouter") -> tuple[int, list[dict[str, Any]] | str]:
    """Run a WISP Wi-Fi scan and return the networks found.

    Returns (status_code, result). On success result is a list of dicts
    {ssid, bssid, band, channel, signal_dbm, encrypted}; on failure it is an
    error message, like the existing set_*_setting() methods.
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{api.base_url}/cgi-bin/luci/admin/network/wireless/wds",
    }

    # The same page set_wisp_setting() uses for the CSRF token.
    token_html = api.get(
        "admin/network/wireless/wds/config?nomodal=&mode=wisp", True
    )
    token = _extract_hidden(token_html, "token")
    if not token:
        return 0, "No CSRF token found (login session expired?)"

    post_resp = api._luci_post(
        "admin/network/wireless/wds/doscan",
        timeout=DEFAULT_POST_TIMEOUT,
        headers={
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": api.base_url,
        },
        data=urllib.parse.urlencode({"token": token, "timeclock": str(int(time.time()))}),
    )
    if not post_resp:
        return 0, "Could not start the WISP scan (doscan failed)"

    scan_resp = api._luci_get(
        "admin/network/wireless/wds/scan?embedded=&nomodal=&mode=wisp",
        timeout=DEFAULT_GET_TIMEOUT,
        headers=headers,
    )
    if not scan_resp:
        return 0, "Could not load the WISP scan result"

    networks = _parse_wisp_scan_networks(scan_resp.text)
    return scan_resp.status_code, networks


def join_wisp(
    api: "CudyRouter",
    ssid: str,
    bssid: str,
    key: str,
    hostname: str | None = None,
    proto: str | None = None,
) -> tuple[int, str]:
    """Join the given Wi-Fi network as the WISP uplink.

    Uses _submit_form() like the existing set_*_setting() methods: GET the
    pre-filled join page (ssid/bssid/iface as query parameters), override the
    password, POST it back and run the apply workflow. The protocol and
    hostname keep the router's pre-filled values unless given.
    """
    quoted_ssid = urllib.parse.quote(ssid)
    quoted_bssid = urllib.parse.quote(bssid)
    fetch_path = (
        "admin/network/wireless/wds/join"
        f"?embedded=&nomodal=&mode=wisp&ssid={quoted_ssid}&bssid={quoted_bssid}&iface={WISP_IFACE}"
    )
    overrides = {"cbid.wireless.wdstmp.key": key or ""}
    if proto:
        overrides["cbid.network.wisp.proto"] = proto
    if hostname:
        overrides["cbid.network.wisp.hostname"] = hostname
    return api._submit_form(
        fetch_path,
        overrides,
        referer=f"{api.base_url}/cgi-bin/luci/admin/network/wireless/wds",
    )
