"""WISP Scan & Connect - Erweiterung für die cudy_router Integration.

Neue Datei, die in `custom_components/cudy_router/` abgelegt wird (gleiche
Ebene wie `router.py`, `select.py`, `button.py`). Enthält die Funktionen für
WLAN-Scan und -Connect im WISP-Uplink-Modus, die im offiziellen Repo
(usersaynoso/ha-cudy-router) bisher fehlen - laut Quellcode-Analyse bietet
`set_wisp_setting()` nur `enabled` (An/Aus), aber keinen Scan oder Connect.

Reverse-Engineered am 2026-07-11 live gegen Firmware 2.5.12-20260518-234632
per Browser-Session (siehe Bau-Logbuch in
'1 - Projekte/Smarter Caravan/Smarter Caravan.md').

Nutzt bewusst die vorhandene Session-/Token-/Retry-Infrastruktur der
`CudyRouter`-Klasse (`_luci_get`, `_luci_post`, `_submit_form`, `get`,
`_extract_hidden`) statt eigene HTTP-Logik zu duplizieren - dieselben
Konventionen wie die bestehenden `set_*_setting()`-Methoden in router.py.

API-Ablauf (aus dem Netzwerk-Log der echten LuCI-Oberfläche):
1. POST admin/network/wireless/wds/doscan          -> startet Scan (blockiert ~15s)
2. GET  admin/network/wireless/wds/scan?embedded=&nomodal=&mode=wisp
        -> HTML-Tabelle mit gefundenen Netzen
3. GET  admin/network/wireless/wds/join?embedded=&nomodal=&mode=wisp&ssid=..&bssid=..&iface=wlan00
        -> vorausgefülltes Formular (token, cbid.wireless.wdstmp.ssid/key/iface,
           cbid.network.wisp.proto/hostname)
4. POST derselbe Pfad mit Passwort-Override -> verbindet (inkl. Apply-Workflow,
        das `_submit_form()` bereits automatisch mit abhandelt)
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
    """Gleiche Regex wie router.py::_extract_hidden (dupliziert, um router.py
    nicht anfassen zu müssen)."""
    match = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return match.group(1) if match else ""


def _dedupe_cell_text(text: str) -> str:
    """Die Scan-Tabelle enthält pro Zelle ein Accessibility-Duplikat (sichtbarer
    + versteckter Screenreader-Text), das BeautifulSoups get_text() ohne
    Trennzeichen zusammenklebt - z.B. "Zeeuwse Kust – FreeZeeuwse Kust – Free"
    statt "Zeeuwse Kust – Free". Erkennt exakte Verdopplung (gerade Länge,
    beide Hälften identisch) und kürzt auf die erste Hälfte."""
    n = len(text)
    if n > 0 and n % 2 == 0:
        half = n // 2
        if text[:half] == text[half:]:
            return text[:half]
    return text


def _parse_wisp_scan_networks(html: str) -> list[dict[str, Any]]:
    """Parst die Scan-Ergebnis-Tabelle der WISP-Seite.

    Jede Datenzeile hat 9 <td>: [No, Band, BSSID, SSID(lang), SSID(kurz),
    Kanal, Signal, -, Verbinden-Button]. Ein <i class="icon icon-lock">
    markiert verschlüsselte Netze, "icon-none" offene.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-striped")
    if table is None:
        _LOGGER.debug("WISP scan: kein Ergebnis-Table im HTML gefunden")
        return []

    networks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 9:
            continue  # Header- oder Spacer-Zeile

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
    """Startet einen WISP-WLAN-Scan und liefert die gefundenen Netze.

    Rückgabe (status_code, ergebnis) - bei Erfolg ist ergebnis eine Liste von
    dicts {ssid, bssid, band, channel, signal_dbm, encrypted}, bei Fehler ein
    Fehlertext (analog zu den bestehenden set_*_setting()-Methoden).
    """
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"{api.base_url}/cgi-bin/luci/admin/network/wireless/wds",
    }

    # Dieselbe Seite, die auch set_wisp_setting() in router.py für den
    # CSRF-Token nutzt (admin/setup redirectete im Backend-Request ohne
    # Browser-Session teils ohne Token-Feld - dieser Pfad ist im Original
    # bereits als zuverlässig erprobt).
    token_html = api.get(
        "admin/network/wireless/wds/config?nomodal=&mode=wisp", True
    )
    token = _extract_hidden(token_html, "token")
    if not token:
        return 0, "Kein CSRF-Token gefunden (Login-Session abgelaufen?)"

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
        return 0, "WISP-Scan konnte nicht gestartet werden (doscan fehlgeschlagen)"

    scan_resp = api._luci_get(
        "admin/network/wireless/wds/scan?embedded=&nomodal=&mode=wisp",
        timeout=DEFAULT_GET_TIMEOUT,
        headers=headers,
    )
    if not scan_resp:
        return 0, "WISP-Scan-Ergebnis konnte nicht geladen werden"

    networks = _parse_wisp_scan_networks(scan_resp.text)
    return scan_resp.status_code, networks


def join_wisp(
    api: "CudyRouter",
    ssid: str,
    bssid: str,
    key: str,
    hostname: str = "LT300",
    proto: str = "dhcp",
) -> tuple[int, str]:
    """Verbindet den WISP-Uplink mit dem angegebenen WLAN-Netz.

    Nutzt _submit_form() wie die bestehenden set_*_setting()-Methoden: GET auf
    die vorausgefüllte Join-Seite (ssid/bssid/iface via Query-Param), Overrides
    fürs Passwort/Protokoll/Hostname drüberlegen, POST zurück - inkl. dem in
    _submit_form() bereits eingebauten Apply-Workflow (Warten auf
    "Konfiguration angewendet").
    """
    quoted_ssid = urllib.parse.quote(ssid)
    quoted_bssid = urllib.parse.quote(bssid)
    fetch_path = (
        "admin/network/wireless/wds/join"
        f"?embedded=&nomodal=&mode=wisp&ssid={quoted_ssid}&bssid={quoted_bssid}&iface={WISP_IFACE}"
    )
    overrides = {
        "cbid.wireless.wdstmp.key": key or "",
        "cbid.network.wisp.proto": proto,
        "cbid.network.wisp.hostname": hostname,
    }
    return api._submit_form(
        fetch_path,
        overrides,
        referer=f"{api.base_url}/cgi-bin/luci/admin/network/wireless/wds",
    )
