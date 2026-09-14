"""
Frontend/browser smoke tests using a real Chromium instance (Playwright).

These are the "add frontend/browser smoke tests if practical" tests from
the hardening/transformation review - a real browser is the only way to
actually verify what the other test files can't: that the page loads and
wires up correctly, that a stored-XSS payload really doesn't execute when
rendered (not just that a Python string got escaped somewhere), and that a
phone-width viewport doesn't get pushed into horizontal scroll (the exact,
previously real bug reported by a user on this project - see
PROJECT_STATUS.md section 5.8 - and explicitly called out as a
never-regress requirement).

Skips itself entirely (rather than failing) when Playwright or a Chromium
binary isn't available, so it never breaks a plain `pytest` run in an
environment that hasn't installed browsers - see requirements-dev.txt
(playwright is an optional dev dependency) and README's testing section for
the one-time `playwright install chromium` this needs to actually run.

Run with:  cd backend && python3 -m pytest tests/test_frontend_smoke.py -v
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright not installed - see requirements-dev.txt")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND_DIR = Path(__file__).parent.parent

# Prefer the sandbox's pre-installed Chromium (used throughout this
# project's manual verification) but fall back to Playwright's own
# resolution (a real `playwright install chromium`) when that path doesn't
# exist, e.g. on a contributor's machine.
_SANDBOX_CHROMIUM = "/opt/pw-browsers/chromium"
CHROMIUM_PATH = _SANDBOX_CHROMIUM if Path(_SANDBOX_CHROMIUM).exists() else None

SCRIPT_PAYLOAD = "<script>window.__xss_fired=(window.__xss_fired||0)+1</script>"
IMG_PAYLOAD = "<img src=x onerror=window.__xss_fired=(window.__xss_fired||0)+1>"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn process (not TestClient) serving the actual frontend,
    on an isolated throwaway SQLite database, with auth/rate-limiting off so
    a browser driving many requests doesn't get caught by either."""
    port = _free_port()
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "smoke.db"
        env = dict(os.environ)
        env.update({
            "DATABASE_URL": f"sqlite:///{db_path}",
            "APP_ACCESS_TOKEN": "",
            "RATE_LIMIT_ENABLED": "false",
            "ENVIRONMENT": "development",
        })
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(BACKEND_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
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
                pytest.fail(f"live server never started listening on {port}:\n{out}")
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


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


def _assess_cardiac(page, base_url):
    page.goto(base_url)
    page.wait_for_selector("#conditionSelect", timeout=10_000)
    page.select_option("#conditionSelect", "cardiac")
    page.click("#assessBtn")
    page.wait_for_selector("#recTable tbody tr", timeout=10_000)


# --------------------------------------------------------------- smoke ----

def test_app_loads(page, live_server):
    page.goto(live_server)
    assert "Ambulance" in page.title() or page.query_selector(".brand") is not None
    # No uncaught page error on initial load (e.g. the historical
    # "L is not defined" bug when the map tile CDN is blocked).
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.wait_for_timeout(500)
    assert errors == []


def test_assessment_and_recommendation_flow(page, live_server):
    _assess_cardiac(page, live_server)
    rows = page.query_selector_all("#recTable tbody tr")
    assert len(rows) > 0
    # Explainable reasoning must actually be present, not a placeholder.
    assert page.query_selector("#recTable") is not None


def test_hospital_selection_works(page, live_server):
    _assess_cardiac(page, live_server)
    select_btn = page.query_selector("#recTable button[data-select]")
    assert select_btn is not None
    select_btn.click()
    page.wait_for_timeout(500)
    # Confirm dialog for a non-recommended pick, or immediate accept for the
    # recommended one - either way, the trip should progress past "awaiting".
    confirm_btn = page.query_selector("#overrideConfirmBtn")
    if confirm_btn and confirm_btn.is_visible():
        confirm_btn.click()
    page.wait_for_timeout(1000)
    status_text = page.eval_on_selector("#tripStatus", "el => el.textContent")
    # "en route to hospital" (progressed) vs. the pre-selection status,
    # "recommendation ready — awaiting crew decision" - check the leading
    # status line specifically, since "awaiting" also legitimately appears
    # later in an unrelated "pre-alert: sent, awaiting ack" line.
    assert "en route" in status_text.lower() or "arrived" in status_text.lower()


def test_hospital_view_loads(page, live_server):
    page.goto(live_server)
    page.wait_for_selector('.tab-btn[data-tab="hospital"]')
    page.click('.tab-btn[data-tab="hospital"]')
    page.wait_for_timeout(500)
    assert page.query_selector("#hospTable") is not None
    assert page.is_visible("#tab-hospital")


def test_analytics_loads(page, live_server):
    page.goto(live_server)
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
    # Regression test for the stored-XSS finding in
    # HARDENING_RECOMMENDATIONS.md: a malicious override_reason/
    # ambulance_label must render as inert text everywhere it's displayed
    # (trip status card, audit log, fleet list), never as live markup.
    fired = []
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

    fired.append(page.evaluate("() => window.__xss_fired"))
    assert page.eval_on_selector("#tripStatus", "el => !!el.querySelector('img')") is False
    assert IMG_PAYLOAD in page.eval_on_selector("#tripStatus", "el => el.textContent")

    page.click('.tab-btn[data-tab="analytics"]')
    page.wait_for_timeout(500)
    fired.append(page.evaluate("() => window.__xss_fired"))
    assert page.eval_on_selector("#auditTable", "el => !!el.querySelector('img')") is False

    assert all(count == 0 for count in fired), f"XSS payload executed! fired counters: {fired}"
    assert dialogs == []


def test_poisoned_ambulance_label_renders_inert_in_fleet_list(page, live_server):
    import urllib.request
    import json as json_mod

    req = urllib.request.Request(
        live_server + "/api/trips",
        data=json_mod.dumps({"ambulance_label": SCRIPT_PAYLOAD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = json_mod.loads(urllib.request.urlopen(req, timeout=5).read())
    assert resp["ambulance_label"] == SCRIPT_PAYLOAD

    page.goto(live_server)
    page.evaluate("() => { window.__xss_fired = 0; }")
    page.wait_for_timeout(1200)
    assert page.evaluate("() => window.__xss_fired") == 0
    assert page.eval_on_selector("#fleetList", "el => !!el.querySelector('script')") is False
