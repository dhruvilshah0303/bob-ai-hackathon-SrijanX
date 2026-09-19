"""
Frontend/browser smoke tests using a real Chromium instance (Playwright).

These are the tests that verify what the other test files can't: that the
real dispatcher console (not a marketing page) is what's actually served at
"/", that the login gate genuinely gates the app, that a stored-XSS payload
really doesn't execute when rendered (not just that a Python string got
escaped somewhere), and that a phone-width viewport doesn't get pushed into
horizontal scroll.

Skips itself entirely (rather than failing) when Playwright or a Chromium
binary isn't available, so it never breaks a plain `pytest` run in an
environment that hasn't installed browsers - see requirements-dev.txt
(playwright is an optional dev dependency) and README's testing section for
the one-time `playwright install chromium` this needs to actually run.

The incident-location step in every flow below uses the manual latitude/
longitude inputs (#incidentLatInput/#incidentLngInput/#incidentCoordSetBtn)
rather than clicking the map: the map tiles/script load from an external
CDN, which may be unreachable in a locked-down CI/sandbox network, and the
app is deliberately built so that failure never blocks creating an
emergency (see app.js's setPendingIncident) - these tests exercise that
same always-available path rather than depending on the CDN being reachable.

Run with:  cd backend && python3 -m pytest tests/test_frontend_smoke.py -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright not installed - see requirements-dev.txt")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND_DIR = Path(__file__).parent.parent

_SANDBOX_CHROMIUM = "/opt/pw-browsers/chromium"
CHROMIUM_PATH = _SANDBOX_CHROMIUM if Path(_SANDBOX_CHROMIUM).exists() else None

SCRIPT_PAYLOAD = "<script>window.__xss_fired=(window.__xss_fired||0)+1</script>"
IMG_PAYLOAD = "<img src=x onerror=window.__xss_fired=(window.__xss_fired||0)+1>"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "smoke-test-admin-pw"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_live_server(extra_env: dict):
    port = _free_port()
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "smoke.db"
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
        "APP_ACCESS_TOKEN": "",
        "RATE_LIMIT_ENABLED": "false",
        "ENVIRONMENT": "development",
        "ADMIN_USERNAME": ADMIN_USERNAME,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
    })
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    up = False
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                up = True
                break
        except OSError:
            time.sleep(0.3)
    if not up:
        out = proc.stdout.read() if proc.stdout else ""
        proc.kill()
        pytest.fail(f"live server never started listening on {port}:\n{out}")
    return proc, base_url


def _stop_live_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn process (not TestClient) serving the actual frontend,
    on an isolated throwaway SQLite database, with rate-limiting off so a
    browser driving many requests doesn't get caught by it, and
    REQUIRE_LOGIN=true so the login-gate/RBAC behavior below is actually
    exercised (the app's real default is REQUIRE_LOGIN=false - open access,
    no gate - see the open_access_server fixture/tests further down)."""
    proc, base_url = _spawn_live_server({"REQUIRE_LOGIN": "true"})
    try:
        yield base_url
    finally:
        _stop_live_server(proc)


@pytest.fixture(scope="module")
def open_access_server():
    """Same as live_server, but with the app's actual default: no
    REQUIRE_LOGIN set, so the login gate should never appear and every
    request should just work."""
    proc, base_url = _spawn_live_server({})
    try:
        yield base_url
    finally:
        _stop_live_server(proc)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        launch_kwargs = {"headless": True}
        if CHROMIUM_PATH:
            launch_kwargs["executable_path"] = CHROMIUM_PATH
        try:
            b = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # pragma: no cover - environment-dependent
            pytest.skip(f"could not launch a Chromium browser: {exc}")
            return
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def _api_login_token(base_url: str, username=ADMIN_USERNAME, password=ADMIN_PASSWORD) -> str:
    req = urllib.request.Request(
        base_url + "/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())["token"]


def _login(page, base_url, username=ADMIN_USERNAME, password=ADMIN_PASSWORD):
    page.goto(base_url)
    page.wait_for_selector("#loginUsername", timeout=10_000)
    page.fill("#loginUsername", username)
    page.fill("#loginPassword", password)
    page.click("#loginSubmit")
    page.wait_for_selector("#createEmergencyCard:not([hidden])", timeout=10_000)


def _log_incident(page, lat=23.03, lng=72.56):
    page.fill("#incidentLatInput", str(lat))
    page.fill("#incidentLngInput", str(lng))
    page.click("#incidentCoordSetBtn")
    page.wait_for_function("() => !document.querySelector('#createEmergencyBtn').disabled", timeout=5_000)
    page.click("#createEmergencyBtn")
    page.wait_for_selector("#assessCard:not([hidden])", timeout=10_000)


def _assess_cardiac(page, base_url):
    _login(page, base_url)
    _log_incident(page)
    page.select_option("#conditionSelect", "cardiac")
    page.click("#assessBtn")
    page.wait_for_selector("#recTable tbody tr", timeout=10_000)


# --------------------------------------------------------------- smoke ----

def test_login_gate_blocks_the_app_until_authenticated(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#loginUsername", timeout=10_000)
    # The full-screen login overlay must be showing and not yet dismissed -
    # everything else on the page is behind it until a real login succeeds.
    assert "hidden" not in (page.get_attribute("#authGate", "class") or "")
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.wait_for_timeout(300)
    assert errors == []


def test_login_with_wrong_password_shows_an_error(page, live_server):
    page.goto(live_server)
    page.wait_for_selector("#loginUsername", timeout=10_000)
    page.fill("#loginUsername", ADMIN_USERNAME)
    page.fill("#loginPassword", "definitely-wrong")
    page.click("#loginSubmit")
    page.wait_for_timeout(500)
    assert "failed" in page.eval_on_selector("#authGateError", "el => el.textContent").lower() \
        or page.eval_on_selector("#authGateError", "el => el.textContent").strip() != ""


def test_app_loads_the_real_console_not_a_marketing_page(page, live_server):
    _login(page, live_server)
    assert "Ambulance" in page.title() or page.query_selector(".brand") is not None
    assert page.query_selector("#conditionSelect") is not None
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.wait_for_timeout(500)
    assert errors == []


def test_assessment_and_recommendation_flow(page, live_server):
    _assess_cardiac(page, live_server)
    rows = page.query_selector_all("#recTable tbody tr")
    assert len(rows) > 0
    assert page.query_selector("#recTable") is not None


def test_ai_triage_assist_prefills_the_condition(page, live_server):
    _login(page, live_server)
    _log_incident(page)
    page.fill("#triageNoteInput", "crushing chest pain, sweating, radiating to left arm")
    page.click("#triageSuggestBtn")
    page.wait_for_selector("#triageSuggestionResult:not(.hidden)", timeout=10_000)
    assert page.eval_on_selector("#conditionSelect", "el => el.value") == "cardiac"
    assert not page.eval_on_selector("#assessBtn", "el => el.disabled")


def test_hospital_selection_works(page, live_server):
    _assess_cardiac(page, live_server)
    select_btn = page.query_selector("#recTable button[data-select]")
    assert select_btn is not None
    select_btn.click()
    page.wait_for_timeout(500)
    confirm_btn = page.query_selector("#overrideConfirmBtn")
    if confirm_btn and confirm_btn.is_visible():
        confirm_btn.click()
    page.wait_for_timeout(1000)
    status_text = page.eval_on_selector("#tripStatus", "el => el.textContent")
    assert "en route" in status_text.lower() or "arrived" in status_text.lower()


def test_hospital_view_loads(page, live_server):
    _login(page, live_server)
    page.wait_for_selector('.tab-btn[data-tab="hospital"]')
    page.click('.tab-btn[data-tab="hospital"]')
    page.wait_for_timeout(500)
    assert page.query_selector("#hospTable") is not None
    assert page.is_visible("#tab-hospital")


def test_admin_can_manage_hospital_capacity_from_the_ui(page, live_server):
    _login(page, live_server)
    page.click('.tab-btn[data-tab="hospital"]')
    page.wait_for_timeout(500)
    assert page.get_attribute("#capacityManageCard", "hidden") is None
    page.fill("#capIcuFree", "1")
    page.click("#capacitySaveBtn")
    page.wait_for_timeout(800)
    # Reflected back in the read-only table below.
    table_text = page.eval_on_selector("#hospTable tbody", "el => el.textContent")
    assert "1" in table_text


def test_analytics_loads(page, live_server):
    _login(page, live_server)
    page.click('.tab-btn[data-tab="analytics"]')
    page.wait_for_timeout(500)
    assert page.is_visible("#tab-analytics")
    assert page.query_selector("#auditTable") is not None


@pytest.mark.parametrize("width,height", [(375, 812), (390, 844), (414, 896)])
def test_mobile_no_horizontal_overflow(page, live_server, width, height):
    # Regression test for a real bug a user hit: a phone-width viewport got
    # pushed into page-level horizontal scroll by the recommendation table,
    # hiding the Accept/Choose button off-screen. Never allowed to return.
    page.set_viewport_size({"width": width, "height": height})
    _assess_cardiac(page, live_server)
    scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
    client_width = page.evaluate("() => document.documentElement.clientWidth")
    assert scroll_width <= client_width + 1, (
        f"page overflows horizontally at {width}px: scrollWidth={scroll_width} > clientWidth={client_width}"
    )


def test_stored_xss_payloads_render_inert(page, live_server):
    # Regression test: a malicious override_reason/ambulance_label must
    # render as inert text everywhere it's displayed (trip status card,
    # audit log, fleet list), never as live markup.
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    _assess_cardiac(page, live_server)
    page.evaluate("() => { window.__xss_fired = 0; }")

    non_recommended_btn = page.query_selector("#recTable tbody tr:not(.recommended) button[data-select]")
    assert non_recommended_btn is not None, "need a non-recommended row to trigger the override flow"
    non_recommended_btn.click()
    page.wait_for_timeout(300)
    page.fill("#overrideReasonInput", IMG_PAYLOAD)
    page.click("#overrideConfirmBtn")
    page.wait_for_timeout(800)

    fired = [page.evaluate("() => window.__xss_fired")]
    assert page.eval_on_selector("#tripStatus", "el => !!el.querySelector('img')") is False
    assert IMG_PAYLOAD in page.eval_on_selector("#tripStatus", "el => el.textContent")

    page.click('.tab-btn[data-tab="analytics"]')
    page.wait_for_timeout(500)
    fired.append(page.evaluate("() => window.__xss_fired"))
    assert page.eval_on_selector("#auditTable", "el => !!el.querySelector('img')") is False

    assert all(count == 0 for count in fired), f"XSS payload executed! fired counters: {fired}"
    assert dialogs == []


def test_poisoned_ambulance_label_renders_inert_in_fleet_list(page, live_server):
    token = _api_login_token(live_server)
    req = urllib.request.Request(
        live_server + "/api/trips",
        data=json.dumps({
            "ambulance_label": SCRIPT_PAYLOAD,
            "incident_location": {"lat": 23.03, "lng": 72.56},
        }).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": token},
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert resp["ambulance_label"] == SCRIPT_PAYLOAD

    _login(page, live_server)
    page.evaluate("() => { window.__xss_fired = 0; }")
    page.wait_for_timeout(1200)
    assert page.evaluate("() => window.__xss_fired") == 0
    assert page.eval_on_selector("#fleetList", "el => !!el.querySelector('script')") is False


# ------------------------------------------------- open access (default) --
# REQUIRE_LOGIN defaults to False - these confirm that real, actual default:
# the app opens straight into the console, no login gate, every action just
# works, no "please log in" anywhere.

def test_app_loads_directly_with_no_login_gate_by_default(page, open_access_server):
    page.goto(open_access_server)
    page.wait_for_selector("#createEmergencyCard", timeout=10_000)
    # The gate stays hidden the whole time - never shown, so nothing to
    # dismiss (contrast with live_server's test_login_gate_blocks_the_app).
    assert "hidden" in (page.get_attribute("#authGate", "class") or "")
    assert page.get_attribute("#sessionBadge", "class") and "hidden" in page.get_attribute("#sessionBadge", "class")
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.wait_for_timeout(300)
    assert errors == []


def test_full_emergency_flow_works_with_no_login_at_all(page, open_access_server):
    page.goto(open_access_server)
    page.wait_for_selector("#createEmergencyCard", timeout=10_000)
    _log_incident(page)
    page.select_option("#conditionSelect", "cardiac")
    page.click("#assessBtn")
    page.wait_for_selector("#recTable tbody tr", timeout=10_000)
    rows = page.query_selector_all("#recTable tbody tr")
    assert len(rows) > 0


def test_hospital_capacity_manage_card_visible_with_no_login(page, open_access_server):
    page.goto(open_access_server)
    page.wait_for_selector("#createEmergencyCard", timeout=10_000)
    page.click('.tab-btn[data-tab="hospital"]')
    page.wait_for_timeout(500)
    # Open access is treated as full admin access (see auth.OPEN_ACCESS_IDENTITY) -
    # the capacity-management controls should be usable without ever logging in.
    assert page.get_attribute("#capacityManageCard", "hidden") is None
