"""Runtime tests for rebooting a mesh node through the main router's own reboot form."""

from __future__ import annotations

import urllib.parse
from types import SimpleNamespace

from tests.module_loader import load_cudy_module


router = load_cudy_module("router")

BASE_URL = "http://192.168.10.1"
MAC = "AA:BB:CC:DD:EE:01"
MAC_PLAIN = "AABBCCDDEE01"
FORM_PAGE = f"admin/network/mesh/reboot?client={MAC_PLAIN}"
APPLY_STEP = f"admin/network/mesh/apply?command=reboot&client={MAC_PLAIN}"


def _form_html(client: str = MAC_PLAIN, token: str | None = "tok123") -> str:
    token_input = f'<input type="hidden" name="token" value="{token}" />' if token else ""
    return (
        f"{token_input}"
        '<input type="hidden" name="timeclock" value="1760000000" />'
        f'<input type="hidden" name="cbid.reboot.1.client" value="{client}" />'
    )


def _redirect(step: str = APPLY_STEP) -> SimpleNamespace:
    return SimpleNamespace(status_code=302, text="", headers={"Location": f"{BASE_URL}/cgi-bin/luci/{step}"})


def _applied(text: str = "<div>Configuration applied.</div>") -> SimpleNamespace:
    return SimpleNamespace(status_code=200, text=text, headers={})


class _FakeRouter:
    """Stands in for CudyRouter's LuCI helpers and records every request."""

    reboot_mesh_device = router.CudyRouter.reboot_mesh_device

    def __init__(self, form_html: str, post_response, apply_response) -> None:
        self.base_url = BASE_URL
        self.auth_cookie = "sysauth=abc"
        self.calls: list[tuple[str, str, str | None]] = []
        self._form_html = form_html
        self._post_response = post_response
        self._apply_response = apply_response

    def authenticate(self) -> bool:
        return True

    def _luci_get(self, path, *, timeout=None, headers=None, silent=False):
        self.calls.append(("GET", path, None))
        if path == FORM_PAGE:
            return SimpleNamespace(status_code=200, text=self._form_html, headers={})
        if path.startswith("admin/network/mesh/apply?"):
            return self._apply_response
        return None

    def _luci_post(self, path, *, timeout=None, headers=None, data=None, silent=False):
        self.calls.append(("POST", path, data))
        return self._post_response


def test_mesh_reboot_submits_the_form_and_follows_the_apply_step() -> None:
    """The reboot needs the form's token, the cbi.apply field and the apply step the router redirects to."""
    api = _FakeRouter(_form_html(), _redirect(), _applied())

    status, message = api.reboot_mesh_device(MAC)

    assert status == 200
    assert MAC in message
    assert [call[:2] for call in api.calls] == [("GET", FORM_PAGE), ("POST", FORM_PAGE), ("GET", APPLY_STEP)]
    assert dict(urllib.parse.parse_qsl(api.calls[1][2], keep_blank_values=True)) == {
        "token": "tok123",
        "timeclock": "1760000000",
        "cbi.submit": "1",
        "cbid.reboot.1.client": MAC_PLAIN,
        "cbi.apply": "",
    }


def test_mesh_reboot_refuses_a_form_for_another_node() -> None:
    """Never submit a reboot form that names a different node."""
    api = _FakeRouter(_form_html(client="AABBCCDDEE02"), _redirect(), _applied())

    status, _message = api.reboot_mesh_device(MAC)

    assert status == 0
    assert [call[0] for call in api.calls] == ["GET"]


def test_mesh_reboot_needs_the_form_token() -> None:
    """Without the form's token there is nothing valid to submit."""
    api = _FakeRouter(_form_html(token=None), _redirect(), _applied())

    status, _message = api.reboot_mesh_device(MAC)

    assert status == 0
    assert [call[0] for call in api.calls] == ["GET"]


def test_mesh_reboot_does_not_follow_an_unexpected_redirect() -> None:
    """Only the apply step for this node is followed."""
    api = _FakeRouter(_form_html(), _redirect("admin/system/reboot"), _applied())

    status, _message = api.reboot_mesh_device(MAC)

    assert status == 0
    assert [call[:2] for call in api.calls] == [("GET", FORM_PAGE), ("POST", FORM_PAGE)]


def test_mesh_reboot_reports_failure_when_the_router_does_not_confirm() -> None:
    """HTTP success alone is not proof: the apply step must confirm the configuration was applied."""
    api = _FakeRouter(_form_html(), _redirect(), _applied("<div>Error</div>"))

    status, message = api.reboot_mesh_device(MAC)

    assert status == 0
    assert "did not confirm" in message
