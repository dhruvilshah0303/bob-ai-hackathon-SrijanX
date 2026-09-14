// AI Ambulance-to-Hospital Coordinator — demo frontend
// Talks to the FastAPI backend in ../backend/app.py over REST + a WebSocket
// for live position/recommendation/reroute updates. The backend supports
// any number of concurrent ambulances (trips); this page controls exactly
// one of them ("my" trip) and renders every other active trip alongside it
// so multi-ambulance behavior (hospitals as a shared, contested resource)
// is visible without needing a second browser.

const state = {
  map: null,
  hospitalMarkers: {},
  ambulanceMarkers: {}, // trip_id -> Leaflet marker, includes "my" trip and every autopilot trip
  incidentMarker: null,
  hospitals: {},
  myTripId: null,
  trip: null,           // full trip object for myTripId
  allTrips: {},         // trip_id -> lightweight trip summary (from trips_list_updated), for fleet/map/hospital view
  scenario: null,
  ws: null,
  pendingOverrideHospitalId: null,
};

const BACKEND_ORIGIN = (window.COORDINATOR_BACKEND_URL || "").replace(/\/$/, "");

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------------------------------------------------- Access token ----
// Only relevant when the backend has APP_ACCESS_TOKEN set (see auth.py) -
// when it doesn't, every request succeeds with or without this header and
// the gate below never appears. Stored per-browser in localStorage so you
// only enter it once per device.
function getToken() {
  try {
    return localStorage.getItem("accessToken") || "";
  } catch {
    return "";
  }
}
function setToken(token) {
  try {
    localStorage.setItem("accessToken", token);
  } catch {
    // Private-browsing / storage-disabled: token just won't persist across
    // reloads, which is a minor inconvenience, not a functional break.
  }
}
function authHeaders() {
  const token = getToken();
  return token ? { "X-API-Key": token } : {};
}

let resolveGate = null;
function showAccessGate(errorMessage) {
  const gate = $("#accessGate");
  gate.classList.remove("hidden");
  $("#accessGateError").textContent = errorMessage || "";
  $("#accessTokenInput").value = "";
  $("#accessTokenInput").focus();
  return new Promise((resolve) => { resolveGate = resolve; });
}
function hideAccessGate() {
  $("#accessGate").classList.add("hidden");
}
function setupAccessGate() {
  const submit = () => {
    const value = $("#accessTokenInput").value.trim();
    if (!value) return;
    setToken(value);
    // Deliberately NOT hiding the gate here - we don't yet know if this
    // code is correct. request()'s retry loop hides it on actual success,
    // or re-shows it with an error if it's still wrong. Closing optimistically
    // here was a real bug: a wrong code would strand the page with the gate
    // gone and no way back in short of a manual reload.
    if (resolveGate) { const r = resolveGate; resolveGate = null; r(value); }
  };
  $("#accessTokenSubmit").addEventListener("click", submit);
  $("#accessTokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

// --------------------------------------------------------------- Toasts ----
function toast(message, kind = "info") {
  const stack = $("#toastStack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// ---------------------------------------------------------- Fetch helpers ----
// One shared function so the "prompt for access code on 401, keep prompting
// until it's right" behavior lives in a single place rather than three
// copies. Loops (rather than retrying once) because giving up after one
// wrong attempt strands the user on a broken page with no way back in
// short of a manual reload - a real bug caught in testing.
async function request(url, options = {}) {
  let sawFailedAttempt = false;
  while (true) {
    let r;
    try {
      const requestUrl = url.startsWith("http") ? url : `${BACKEND_ORIGIN}${url}`;
      r = await fetch(requestUrl, {
        ...options,
        headers: { ...(options.headers || {}), ...authHeaders() },
      });
    } catch (e) {
      toast(`Network error reaching the backend: ${e.message}`, "error");
      return null;
    }

    if (r.status === 401) {
      await showAccessGate(sawFailedAttempt ? "Access code incorrect - please try again." : "");
      sawFailedAttempt = true;
      continue; // retry the same request with the newly-entered token
    }

    if (sawFailedAttempt) hideAccessGate(); // this attempt got past auth - close it now

    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      if (r.status === 429) {
        toast(body.detail || "Too many requests - please slow down.", "error");
      } else {
        toast(`Request failed: ${body.detail || r.statusText}`, "error");
      }
      return null;
    }
    return await r.json();
  }
}

async function getJSON(url) {
  return request(url);
}

async function postJSON(url, body) {
  return request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

async function deleteJSON(url) {
  return request(url, { method: "DELETE" });
}

// Wraps a button's click handler so it disables itself while the request is
// in flight — prevents a slow response (more likely now that ETA can hit a
// real traffic API) from inviting a double-click / double-submit.
function withBusyButton(btn, handler) {
  return async (...args) => {
    if (btn.disabled) return;
    const original = btn.textContent;
    btn.disabled = true;
    try {
      await handler(...args);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  };
}

// ---------------------------------------------------------------- Tabs ----
function setupTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "analytics") loadAnalytics();
      if (btn.dataset.tab === "hospital") renderHospitalView();
      if (state.map) setTimeout(() => state.map.invalidateSize(), 50);
    });
  });
}

// ----------------------------------------------------------------- Map ----
function initMap(incident) {
  state.map = L.map("map", { zoomControl: true }).setView([incident.lat, incident.lng], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);

  const incidentIcon = L.divIcon({
    className: "",
    html: '<div style="font-size:22px">📍</div>',
    iconSize: [24, 24],
    iconAnchor: [12, 22],
  });
  state.incidentMarker = L.marker([incident.lat, incident.lng], { icon: incidentIcon })
    .addTo(state.map)
    .bindPopup("Incident scene (my ambulance)");
}

function ambulanceIcon(isMine) {
  return L.divIcon({
    className: "",
    html: `<div style="font-size:${isMine ? 26 : 20}px">🚑</div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

function upsertAmbulanceMarker(tripId, pos, label, isMine) {
  if (!pos || !state.map) return;
  if (state.ambulanceMarkers[tripId]) {
    state.ambulanceMarkers[tripId].setLatLng([pos.lat, pos.lng]);
  } else {
    const marker = L.marker([pos.lat, pos.lng], { icon: ambulanceIcon(isMine) })
      .addTo(state.map)
      .bindPopup(label);
    state.ambulanceMarkers[tripId] = marker;
  }
}

function removeAmbulanceMarker(tripId) {
  const m = state.ambulanceMarkers[tripId];
  if (m && state.map) {
    state.map.removeLayer(m);
    delete state.ambulanceMarkers[tripId];
  }
}

function hospitalStatusColor(h) {
  if (h.accepting_status === "no" || h.icu_beds_free === 0) return "#ff5d5d";
  if (h.accepting_status === "limited") return "#ffb84d";
  return "#2fd1a3";
}

function renderHospitalsOnMap(hospitals) {
  if (!state.map) return;
  Object.values(hospitals).forEach((h) => {
    const color = hospitalStatusColor(h);
    if (state.hospitalMarkers[h.id]) {
      state.hospitalMarkers[h.id].setStyle({ color, fillColor: color });
      state.hospitalMarkers[h.id].setPopupContent(hospitalPopupHtml(h));
    } else {
      const marker = L.circleMarker([h.lat, h.lng], {
        radius: 9,
        color,
        fillColor: color,
        fillOpacity: 0.85,
        weight: 2,
      })
        .addTo(state.map)
        .bindPopup(hospitalPopupHtml(h));
      state.hospitalMarkers[h.id] = marker;
    }
  });
}

function hospitalPopupHtml(h) {
  const specs = h.specialists.filter((s) => s.on_duty).map((s) => s.type).join(", ") || "none on duty";
  return `<b>${h.name}</b><br/>ICU free: ${h.icu_beds_free}/${h.icu_beds_total}<br/>
    ED: ${h.ed_bays_occupied}/${h.ed_bays_total} occupied<br/>
    Accepting: ${h.accepting_status}<br/>Specialists: ${specs}`;
}

// --------------------------------------------------------- Recommendation ----
function loadStatusBadge(load) {
  if (load === "overloaded") return `<span class="badge bad">overloaded</span>`;
  if (load === "busy") return `<span class="badge warn">busy</span>`;
  return `<span class="badge ok">normal</span>`;
}

function renderRecommendation(trip) {
  const rec = trip.recommendation;
  if (!rec) {
    $("#recommendationCard").hidden = true;
    return;
  }
  $("#recommendationCard").hidden = false;
  $("#conditionLabel").textContent = `— ${rec.condition_label}`;

  const tbody = $("#recTable tbody");
  tbody.innerHTML = "";

  rec.candidates.forEach((c) => {
    const isRecommended = c.hospital_id === rec.recommended_hospital_id;
    const tr = document.createElement("tr");
    tr.className = (isRecommended ? "recommended " : "") + (!c.viable ? "not-viable" : "");

    const icuBadge = c.icu_available
      ? `<span class="badge ok">available</span>`
      : `<span class="badge bad">unavailable</span>`;
    const specBadge = rec.preferred_specialist
      ? c.specialist_match
        ? `<span class="badge ok">${rec.preferred_specialist.replace("_", " ")}</span>`
        : `<span class="badge bad">not on duty</span>`
      : `<span class="muted">n/a</span>`;

    const actionCell = trip.status === "en_route" || trip.status === "arrived"
      ? ""
      : `<button class="btn btn-small ${isRecommended ? "btn-primary" : "btn-ghost"}" data-select="${c.hospital_id}">
           ${isRecommended ? "Accept" : "Choose"}
         </button>`;

    tr.innerHTML = `
      <td><b>${c.name}</b>${isRecommended ? '<span class="badge star">★ RECOMMENDED</span>' : ""}</td>
      <td>${c.distance_km != null ? c.distance_km.toFixed(1) + " km" : "—"}</td>
      <td>${c.eta_min != null ? Math.round(c.eta_min) + " min" : "—"}</td>
      <td>${icuBadge}</td>
      <td>${specBadge}</td>
      <td>${loadStatusBadge(c.ed_load_state)}</td>
      <td>${c.viable ? Math.round(c.est_total_treatment_delay_min) + " min" : "not viable"}</td>
      <td>${actionCell}</td>
    `;
    tbody.appendChild(tr);

    const reasonTr = document.createElement("tr");
    reasonTr.className = "reasons-row";
    reasonTr.innerHTML = `<td colspan="8">${c.reasons.join(" · ")}</td>`;
    tbody.appendChild(reasonTr);
  });

  tbody.querySelectorAll("[data-select]").forEach((btn) => {
    btn.addEventListener("click", withBusyButton(btn, async () => {
      const hospitalId = btn.dataset.select;
      const isOverride = hospitalId !== rec.recommended_hospital_id;
      if (isOverride) {
        openOverrideCard(hospitalId);
        return;
      }
      await selectHospital(hospitalId, null);
    }));
  });
}

function openOverrideCard(hospitalId) {
  state.pendingOverrideHospitalId = hospitalId;
  $("#overrideCard").hidden = false;
  $("#overrideReasonInput").value = "";
  $("#overrideReasonInput").focus();
}

function closeOverrideCard() {
  state.pendingOverrideHospitalId = null;
  $("#overrideCard").hidden = true;
}

async function selectHospital(hospitalId, overrideReason) {
  const trip = await postJSON(`/api/trips/${state.myTripId}/select`, {
    hospital_id: hospitalId,
    override_reason: overrideReason,
  });
  if (trip) {
    state.trip = trip;
    renderAll();
  }
}

// ------------------------------------------------------------ Trip status ----
function statusLabel(s) {
  return {
    awaiting_assessment: "Awaiting patient assessment",
    recommendation_ready: "Recommendation ready — awaiting crew decision",
    en_route: "En route to hospital",
    arrived: "Arrived at hospital",
  }[s] || s;
}

function renderTripStatus(trip) {
  const card = $("#tripCard");
  if (trip.status === "awaiting_assessment") {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const dest = trip.dest_hospital_id ? state.hospitals[trip.dest_hospital_id] : null;
  const pre = trip.prealert;

  let html = `<div class="status-line"><b>${statusLabel(trip.status)}</b></div>`;
  if (dest) {
    html += `<div class="status-line">Destination: <b>${dest.name}</b> ${trip.selection_type === "overridden" ? '<span class="badge warn">override</span>' : '<span class="badge ok">AI recommended</span>'}</div>`;
  }
  if (trip.override_reason) {
    html += `<div class="status-line">Override reason: "${trip.override_reason}"</div>`;
  }
  if (pre) {
    html += `<div class="status-line">Pre-alert: ${pre.acknowledged_at ? '<span class="badge ok">acknowledged by hospital</span>' : '<span class="badge warn">sent, awaiting ack</span>'}</div>`;
  }
  $("#tripStatus").innerHTML = html;
}

function renderRerouteBanner(trip) {
  const banner = $("#rerouteBanner");
  if (!trip.reroute_alert) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  const suggestedId = trip.reroute_alert.snapshot.recommended_hospital_id;
  const suggested = state.hospitals[suggestedId];
  banner.querySelector(".reroute-text").innerHTML =
    `⚠ <b>Reroute suggested</b>: ${trip.reroute_alert.reason}. New recommendation: <b>${suggested ? suggested.name : suggestedId}</b>.`;
}

// -------------------------------------------------------------- Fleet view ----
function renderFleetList() {
  const el = $("#fleetList");
  const others = Object.values(state.allTrips).filter((t) => t.id !== state.myTripId);
  if (others.length === 0) {
    el.innerHTML = `<p class="muted">No other ambulances active right now.</p>`;
    return;
  }
  el.innerHTML = others.map((t) => {
    const dest = t.dest_hospital_id ? state.hospitals[t.dest_hospital_id] : null;
    return `<div class="fleet-item">
      <span><b>${t.ambulance_label}</b> ${t.autopilot ? "🤖" : ""}</span>
      <span>${statusLabel(t.status)}${dest ? " → " + dest.name : ""}</span>
    </div>`;
  }).join("");
}

// -------------------------------------------------------------- Hospital view ----
function renderHospitalView() {
  const panel = $("#prealertPanel");
  const alertTrips = Object.values(state.allTrips).filter((t) => t.prealert);

  if (alertTrips.length === 0) {
    panel.innerHTML = `<p class="muted">No active pre-alerts yet. Select a destination hospital in the Dispatcher tab.</p>`;
  } else {
    panel.innerHTML = alertTrips.map((t) => {
      const pre = t.prealert;
      const h = state.hospitals[pre.hospital_id];
      return `<div class="prealert-panel active">
        <h4>🔔 Incoming: ${t.ambulance_label} → ${h ? h.name : pre.hospital_id}</h4>
        <p><b>Condition:</b> ${pre.condition_label || "—"} &nbsp; <b>ETA:</b> ${Math.round(pre.eta_min)} min</p>
        <p><b>Sent at:</b> ${new Date(pre.sent_at).toLocaleTimeString()}</p>
        ${pre.acknowledged_at
          ? `<p class="badge ok">Acknowledged at ${new Date(pre.acknowledged_at).toLocaleTimeString()}</p>`
          : `<button class="btn btn-primary" data-ack="${t.id}">Acknowledge — prepare team</button>`}
      </div>`;
    }).join("");

    panel.querySelectorAll("[data-ack]").forEach((btn) => {
      btn.addEventListener("click", withBusyButton(btn, async () => {
        const tripId = btn.dataset.ack;
        await postJSON(`/api/trips/${tripId}/prealert/acknowledge`);
        // trips_list_updated will refresh state.allTrips and re-render
      }));
    });
  }

  const tbody = $("#hospTable tbody");
  tbody.innerHTML = "";
  Object.values(state.hospitals).forEach((h) => {
    const specs = h.specialists.filter((s) => s.on_duty).map((s) => s.type.replace("_", " ")).join(", ") || "—";
    tbody.innerHTML += `<tr>
      <td><b>${h.name}</b></td>
      <td>${h.ed_bays_occupied}/${h.ed_bays_total}</td>
      <td>${h.icu_beds_free}/${h.icu_beds_total}</td>
      <td>${h.ward_beds_free}/${h.ward_beds_total}</td>
      <td>${h.accepting_status}</td>
      <td>${specs}</td>
      <td class="muted">${new Date(h.last_capacity_update_at).toLocaleTimeString()}</td>
    </tr>`;
  });
}

// -------------------------------------------------------------- Analytics ----
async function loadAnalytics() {
  const [kpis, audit] = await Promise.all([getJSON("/api/analytics"), getJSON("/api/audit")]);
  if (!kpis || !audit) return;
  const grid = $("#kpiGrid");
  grid.innerHTML = `
    <div class="kpi"><div class="value">${kpis.total_cases}</div><div class="label">Cases assessed</div></div>
    <div class="kpi"><div class="value">${kpis.total_selections}</div><div class="label">Destinations selected</div></div>
    <div class="kpi"><div class="value">${Math.round(kpis.override_rate * 100)}%</div><div class="label">AI override rate</div></div>
    <div class="kpi"><div class="value">${kpis.reroute_count}</div><div class="label">Dynamic reroutes accepted</div></div>
    <div class="kpi"><div class="value">${kpis.prealerts_sent}</div><div class="label">Pre-alerts sent</div></div>
    <div class="kpi"><div class="value">${Math.round(kpis.prealert_ack_rate * 100)}%</div><div class="label">Pre-alert ack rate</div></div>
    <div class="kpi"><div class="value">${kpis.active_trips}</div><div class="label">Active ambulances now</div></div>
  `;
  const tbody = $("#auditTable tbody");
  tbody.innerHTML = "";
  audit.slice().reverse().forEach((e) => {
    tbody.innerHTML += `<tr>
      <td class="muted">${new Date(e.created_at).toLocaleTimeString()}</td>
      <td>${e.event_type}</td>
      <td class="muted">${JSON.stringify(e.payload)}</td>
    </tr>`;
  });
}

// -------------------------------------------------------------- Sim controls ----
function populateSelects(conditions, hospitals) {
  const condSel = $("#conditionSelect");
  condSel.innerHTML = conditions.map((c) => `<option value="${c.code}">${c.label} (${c.severity})</option>`).join("");

  const simSel = $("#simHospital");
  simSel.innerHTML = Object.values(hospitals).map((h) => `<option value="${h.id}">${h.name}</option>`).join("");
}

function setupControls() {
  const assessBtn = $("#assessBtn");
  assessBtn.addEventListener("click", withBusyButton(assessBtn, async () => {
    const code = $("#conditionSelect").value;
    const trip = await postJSON(`/api/trips/${state.myTripId}/case`, { condition_code: code });
    if (trip) {
      state.trip = trip;
      renderAll();
    }
  }));

  $("#overrideConfirmBtn").addEventListener("click", async () => {
    const reason = $("#overrideReasonInput").value.trim() || "No reason given";
    const hospitalId = state.pendingOverrideHospitalId;
    closeOverrideCard();
    await selectHospital(hospitalId, reason);
  });
  $("#overrideCancelBtn").addEventListener("click", closeOverrideCard);

  const applySimBtn = $("#applySimBtn");
  applySimBtn.addEventListener("click", withBusyButton(applySimBtn, async () => {
    const hospitalId = $("#simHospital").value;
    const body = {};
    if ($("#simIcuTaken").checked) body.icu_beds_free = 0;
    if ($("#simEdOverload").checked) {
      const h = state.hospitals[hospitalId];
      body.ed_bays_occupied = h.ed_bays_total;
    }
    if ($("#simCardiologist").checked) {
      body.specialist_type = "cardiologist";
      body.specialist_on_duty = true;
    }
    const result = await postJSON(`/api/hospitals/${hospitalId}/capacity`, body);
    if (result) {
      $("#simIcuTaken").checked = false;
      $("#simEdOverload").checked = false;
      $("#simCardiologist").checked = false;
      toast("Capacity change applied.", "success");
    }
  }));

  $("#addSecondAmbBtn").addEventListener("click", withBusyButton($("#addSecondAmbBtn"), async () => {
    const jitter = () => (Math.random() - 0.5) * 0.02; // ~1-2km offset so it's not on top of the incident marker
    const incident = state.scenario.incident_location;
    const trip = await postJSON("/api/trips", {
      autopilot: true,
      incident_location: { lat: incident.lat + jitter(), lng: incident.lng + jitter() },
    });
    if (trip) {
      toast(`${trip.ambulance_label} launched — it will assess, select, and drive itself.`, "success");
    }
  }));

  $("#rerouteAccept").addEventListener("click", withBusyButton($("#rerouteAccept"), async () => {
    const trip = await postJSON(`/api/trips/${state.myTripId}/reroute-response`, { accept: true });
    if (trip) {
      state.trip = trip;
      renderAll();
    }
  }));
  $("#rerouteDecline").addEventListener("click", withBusyButton($("#rerouteDecline"), async () => {
    const trip = await postJSON(`/api/trips/${state.myTripId}/reroute-response`, { accept: false });
    if (trip) {
      state.trip = trip;
      renderAll();
    }
  }));

  $("#resetBtn").addEventListener("click", withBusyButton($("#resetBtn"), async () => {
    Object.keys(state.ambulanceMarkers).forEach(removeAmbulanceMarker);
    state.allTrips = {};
    await postJSON("/api/reset");
    await startMyTrip();
    const hospitals = await getJSON("/api/hospitals");
    if (hospitals) {
      state.hospitals = Object.fromEntries(hospitals.map((h) => [h.id, h]));
      renderHospitalsOnMap(state.hospitals);
    }
    renderAll();
    toast("Demo reset.", "success");
  }));
}

// ------------------------------------------------------------------ WS ----
function connectWs() {
  const token = getToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  const backendUrl = BACKEND_ORIGIN || window.location.origin;
  const wsUrl = new URL("/ws", backendUrl);
  wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
  wsUrl.search = qs;
  state.ws = new WebSocket(wsUrl.toString());
  state.ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === "trip_updated" || msg.type === "position_updated" || msg.type === "reroute_suggested") {
      const isMine = msg.trip_id === state.myTripId;
      if (isMine) {
        state.trip = msg.trip;
        renderAll();
      }
      upsertAmbulanceMarker(msg.trip_id, msg.trip.ambulance_pos, msg.trip.ambulance_label, isMine);
    }

    if (msg.type === "trip_removed") {
      removeAmbulanceMarker(msg.trip_id);
      delete state.allTrips[msg.trip_id];
    }

    if (msg.type === "trips_list_updated") {
      state.allTrips = Object.fromEntries(msg.trips.map((t) => [t.id, t]));
      msg.trips.forEach((t) => upsertAmbulanceMarker(t.id, t.ambulance_pos, t.ambulance_label, t.id === state.myTripId));
      renderFleetList();
      if ($("#tab-hospital").classList.contains("active")) renderHospitalView();
    }

    if (msg.type === "hospitals_updated") {
      state.hospitals = Object.fromEntries(msg.hospitals.map((h) => [h.id, h]));
      renderHospitalsOnMap(state.hospitals);
      renderAll();
    }
  };
  state.ws.onclose = () => setTimeout(connectWs, 1500);
  state.ws.onerror = () => toast("Lost connection to backend — retrying...", "error");
  // Keep-alive ping so the server's `receive_text()` doesn't block forever on idle proxies.
  setInterval(() => {
    if (state.ws.readyState === WebSocket.OPEN) state.ws.send("ping");
  }, 15000);
}

// --------------------------------------------------------------- Render all ----
function renderAll() {
  if (!state.trip) return;
  renderRecommendation(state.trip);
  renderTripStatus(state.trip);
  renderRerouteBanner(state.trip);
  renderFleetList();
  if ($("#tab-hospital").classList.contains("active")) renderHospitalView();
}

// -------------------------------------------------------------------- Init ----
async function startMyTrip() {
  const trip = await postJSON("/api/trips", {});
  if (!trip) return;
  state.myTripId = trip.id;
  state.trip = trip;
  $("#myAmbulanceLabel").textContent = `(${trip.ambulance_label})`;
  upsertAmbulanceMarker(trip.id, trip.ambulance_pos, trip.ambulance_label, true);
}

async function init() {
  setupTabs();
  setupControls();
  setupAccessGate();

  const scenario = await getJSON("/api/scenario");
  if (!scenario) {
    toast("Could not reach the backend. Is uvicorn running?", "error");
    return;
  }
  state.scenario = scenario;
  $("#etaProviderBadge").textContent = `ETA source: ${scenario.eta_provider.active_mode === "live_api" ? "live traffic API" : "simulated (no API key configured)"}`;

  const hospitalsList = await getJSON("/api/hospitals");
  state.hospitals = Object.fromEntries((hospitalsList || []).map((h) => [h.id, h]));

  try {
    initMap(scenario.incident_location);
    renderHospitalsOnMap(state.hospitals);
  } catch (e) {
    // Map rendering (Leaflet, loaded from a CDN) is not essential to the
    // core dispatch/recommendation flow below - if the map tile/JS CDN is
    // unreachable or blocked, degrade gracefully instead of taking the
    // whole app down with it.
    console.error("Map initialization failed - continuing without the map:", e);
    toast("Map could not load (network/CDN issue). The rest of the app will still work.", "error");
  }
  populateSelects(scenario.conditions, state.hospitals);

  await startMyTrip();
  renderAll();
  connectWs();
}

init();
