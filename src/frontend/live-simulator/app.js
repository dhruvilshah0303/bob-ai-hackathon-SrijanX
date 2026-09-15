// AI Ambulance-to-Hospital Coordinator — demo frontend
// Talks to the FastAPI backend in ../backend/app.py over REST + a WebSocket

const state = {
  map: null,
  hospitalMarkers: {},
  ambulanceMarkers: {}, 
  routeLines: {},       
  incidentMarker: null,
  hospitals: {},
  myTripId: null,
  trip: null,           
  allTrips: {},        
  scenario: null,
  ws: null,
  pendingOverrideHospitalId: null,
  simHospitalUserPicked: false, 
  capacityHistory: {},   // hospital_id -> rolling list of {t, icu_free, icu_total, ed_occupied, ed_total} - see recordCapacitySnapshot()
};

const CAPACITY_HISTORY_MAX_POINTS = 30;

const BACKEND_ORIGIN = (window.COORDINATOR_BACKEND_URL || "").replace(/\/$/, "");

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));


function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}


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
  
    if (resolveGate) { const r = resolveGate; resolveGate = null; r(value); }
  };
  $("#accessTokenSubmit").addEventListener("click", submit);
  $("#accessTokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
}

const TOAST_ICON = { info: "ℹ", success: "✓", error: "⚠" };

function toast(message, kind = "info") {
  const stack = $("#toastStack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.textContent = TOAST_ICON[kind] || TOAST_ICON.info;
  const text = document.createElement("span");
  text.textContent = message;
  el.append(icon, text);
  stack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function setConnectionStatus(state_) {
  const dot = document.getElementById("connDot");
  const label = document.getElementById("connLabel");
  if (!dot || !label) return;
  dot.className = `conn-dot ${state_}`;
  label.textContent = state_ === "live" ? "Live" : state_ === "reconnecting" ? "Reconnecting…" : "Offline";
}

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
      continue; 
    }

    if (sawFailedAttempt) hideAccessGate();

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
      .bindPopup(escapeHtml(label));
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


function upsertRouteLine(tripId, points, isMine) {
  if (!state.map || !points || points.length < 2) return;
  const latlngs = points.map((p) => [p.lat, p.lng]);
  if (state.routeLines[tripId]) {
    state.routeLines[tripId].setLatLngs(latlngs);
  } else {
    state.routeLines[tripId] = L.polyline(latlngs, {
      color: isMine ? "#4d9fff" : "#8fa3b3",
      weight: isMine ? 4 : 2,
      opacity: 0.7,
      dashArray: isMine ? null : "4 6",
    }).addTo(state.map);
  }
}

function removeRouteLine(tripId) {
  const line = state.routeLines[tripId];
  if (line && state.map) {
    state.map.removeLayer(line);
  }
  delete state.routeLines[tripId];
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
  return `<b>${escapeHtml(h.name)}</b><br/>ICU free: ${h.icu_beds_free}/${h.icu_beds_total}<br/>
    ED: ${h.ed_bays_occupied}/${h.ed_bays_total} occupied<br/>
    Accepting: ${escapeHtml(h.accepting_status)}<br/>Specialists: ${escapeHtml(specs)}`;
}

function statusCellHtml(c, preferredSpecialist) {
  const rows = [];
  rows.push(c.icu_available
    ? `<span class="badge ok">ICU ✓</span>`
    : `<span class="badge bad">ICU ✕</span>`);
  if (preferredSpecialist) {
    rows.push(c.specialist_match
      ? `<span class="badge ok">Spec. ✓</span>`
      : `<span class="badge bad">Spec. ✕</span>`);
  }
  const loadLabel = c.ed_load_state === "overloaded" ? "ED full" : c.ed_load_state === "busy" ? "ED busy" : "ED ok";
  const loadCls = c.ed_load_state === "overloaded" ? "bad" : c.ed_load_state === "busy" ? "warn" : "ok";
  rows.push(`<span class="badge ${loadCls}">${loadLabel}</span>`);
  return rows.join("");
}


const SCORE_FACTOR_LABELS = {
  delay: "Time-to-treatment",
  specialist: "Specialist match",
  capacity_headroom: "Capacity headroom",
  ed_overload_penalty: "ED overload penalty",
};
const SCORE_FACTOR_ORDER = ["delay", "specialist", "capacity_headroom", "ed_overload_penalty"];


function scoreBreakdownHtml(breakdown) {
  if (!breakdown) return "";
  const rows = SCORE_FACTOR_ORDER.map((key) => {
    const f = breakdown[key];
    if (!f) return "";
    const pct = Math.max(0, Math.min(100, Math.round(f.value * 100)));
    const isPenalty = key === "ed_overload_penalty";
    const cls = isPenalty ? (f.value > 0 ? "bad" : "ok") : "ok";
    return `
      <div class="score-row">
        <div class="score-row-top">
          <span>${escapeHtml(SCORE_FACTOR_LABELS[key])}</span>
          <span class="muted mono">${Math.round(f.weight * 100)}% weight</span>
        </div>
        <div class="score-bar-track"><div class="score-bar-fill ${cls}" style="width:${pct}%"></div></div>
      </div>`;
  }).join("");
  return `
    <details class="score-breakdown">
      <summary>How was this calculated?</summary>
      <div class="score-rows">${rows}</div>
      <p class="hint">These are the recommendation engine's actual weighted factors for this hospital — fixed weights, applied identically to every candidate. Recommendation favors treatment readiness over distance alone.</p>
    </details>`;
}


function runnerUpCompareHtml(rec, top) {
  const runnerUp = rec.candidates.find((c) => c.viable && c.hospital_id !== top.hospital_id);
  if (!runnerUp) return "";
  const etaDiff = (top.eta_min != null && runnerUp.eta_min != null) ? (runnerUp.eta_min - top.eta_min) : null;
  const delayDiff = (top.est_total_treatment_delay_min != null && runnerUp.est_total_treatment_delay_min != null)
    ? (runnerUp.est_total_treatment_delay_min - top.est_total_treatment_delay_min) : null;
  const parts = [];
  if (etaDiff != null && Math.abs(etaDiff) >= 1) {
    parts.push(etaDiff < 0
      ? `<span class="badge ok">+${Math.round(-etaDiff)} min faster arrival</span>`
      : `<span class="badge warn">−${Math.round(etaDiff)} min slower arrival</span>`);
  }
  if (delayDiff != null && Math.abs(delayDiff) >= 1) {
    parts.push(delayDiff > 0
      ? `<span class="badge bad">−${Math.round(delayDiff)} min slower to actual treatment</span>`
      : `<span class="badge ok">+${Math.round(-delayDiff)} min faster to actual treatment</span>`);
  }
  if (!parts.length) return "";
  return `<div class="why-not"><b>Why not ${escapeHtml(runnerUp.name)}?</b> ${parts.join(" ")}</div>`;
}

function renderRecHero(rec) {
  const hero = $("#recHero");
  const top = rec.candidates.find((c) => c.hospital_id === rec.recommended_hospital_id);
  if (!top) {
    hero.innerHTML = `<div class="rec-hero"><div class="rec-hero-name">No viable hospital found for this condition right now.</div></div>`;
    return;
  }
  hero.innerHTML = `
    <div class="rec-hero">
      <div class="rec-hero-label"><span class="icon">★</span> Recommended — fastest to treatment</div>
      <div class="rec-hero-name">${escapeHtml(top.name)}</div>
      <div class="rec-hero-stats">
        <div class="rec-hero-stat">Traffic-aware ETA<b>${top.eta_min != null ? Math.round(top.eta_min) + " min" : "—"}</b></div>
        <div class="rec-hero-stat">Est. treatment delay<b>${top.viable ? Math.round(top.est_total_treatment_delay_min) + " min" : "—"}</b></div>
        <div class="rec-hero-stat">Distance<b>${top.distance_km != null ? top.distance_km.toFixed(1) + " km" : "—"}</b></div>
        <div class="rec-hero-stat">ICU<b>${top.icu_available ? "Available" : "Unavailable"}</b></div>
      </div>
      <div class="rec-hero-reasons">${top.reasons.map(escapeHtml).join(" · ")}</div>
      ${runnerUpCompareHtml(rec, top)}
      ${scoreBreakdownHtml(top.score_breakdown)}
    </div>
  `;
}

function renderRecommendation(trip) {
  const rec = trip.recommendation;
  
  if (!rec || trip.status !== "recommendation_ready") {
    $("#recommendationCard").hidden = true;
    return;
  }
  $("#recommendationCard").hidden = false;
  $("#conditionLabel").textContent = `— ${rec.condition_label}`;
  renderRecHero(rec);

  const tbody = $("#recTable tbody");
  tbody.innerHTML = "";

  rec.candidates.forEach((c) => {
    const isRecommended = c.hospital_id === rec.recommended_hospital_id;
    const tr = document.createElement("tr");
    tr.className = (isRecommended ? "recommended " : "") + (!c.viable ? "not-viable" : "");

    const safeId = escapeHtml(c.hospital_id);
    const actionCell = trip.status === "en_route" || trip.status === "arrived"
      ? ""
      : `<button class="btn btn-small ${isRecommended ? "btn-primary" : "btn-ghost"}" data-select="${safeId}">
           ${isRecommended ? "Accept" : "Choose"}
         </button>`;

    
    tr.innerHTML = `
      <td data-label="Hospital">
        <b>${escapeHtml(c.name)}</b>${isRecommended ? '<span class="badge star">★ RECOMMENDED</span>' : ""}
        <div class="rec-row-sub muted">${c.distance_km != null ? c.distance_km.toFixed(1) + " km away" : ""}</div>
        <button type="button" class="why-toggle" data-why="${safeId}" aria-expanded="false">why?</button>
      </td>
      <td data-label="Delay" class="rec-delay-cell">
        <div class="rec-delay-top">${c.viable ? Math.round(c.est_total_treatment_delay_min) + " min" : "not viable"}</div>
        <div class="rec-row-sub muted">${c.eta_min != null ? Math.round(c.eta_min) + " min drive" : "—"}</div>
      </td>
      <td data-label="Status" class="rec-status-cell">${statusCellHtml(c, rec.preferred_specialist)}</td>
      <td data-label="">${actionCell}</td>
    `;
    tbody.appendChild(tr);

    
    const reasonTr = document.createElement("tr");
    reasonTr.className = "reasons-row hidden";
    reasonTr.dataset.reasonsFor = c.hospital_id;
    reasonTr.innerHTML = `<td colspan="4" data-label="">${c.reasons.map(escapeHtml).join(" · ")}</td>`;
    tbody.appendChild(reasonTr);
  });

  tbody.querySelectorAll("[data-why]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = tbody.querySelector(`[data-reasons-for="${btn.dataset.why}"]`);
      if (!row) return;
      const nowHidden = row.classList.toggle("hidden");
      btn.textContent = nowHidden ? "why?" : "hide";
      btn.setAttribute("aria-expanded", String(!nowHidden));
    });
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


function renderAssessCard(trip) {
  $("#assessCard").hidden = trip.status !== "awaiting_assessment";
}

function statusLabel(s) {
  return {
    awaiting_assessment: "Awaiting patient assessment",
    recommendation_ready: "Recommendation ready — awaiting crew decision",
    en_route: "En route to hospital",
    arrived: "Arrived at hospital",
  }[s] || s;
}

function routeSourceSummary(routeSource) {
  if (routeSource === "live_api") return "following real roads (live routing API)";
  const rp = state.scenario && state.scenario.routing_provider;
  if (rp && (rp.google_configured || rp.mapbox_configured)) {
    return "straight-line estimate — live routing is configured but temporarily unavailable";
  }
  return "straight-line estimate (no live routing API configured)";
}
function routeSourceTechnicalDetail(routeSource) {
  if (routeSource === "live_api") return null;
  const rp = state.scenario && state.scenario.routing_provider;
  if (rp && (rp.google_configured || rp.mapbox_configured) && rp.last_failure_reason) {
    return rp.last_failure_reason;
  }
  return null;
}


let _routingStatusRefreshPending = false;
async function maybeRefreshRoutingProviderStatus() {
  const rp = state.scenario && state.scenario.routing_provider;
  if (!rp || !(rp.google_configured || rp.mapbox_configured) || rp.last_failure_reason || _routingStatusRefreshPending) return;
  _routingStatusRefreshPending = true;
  try {
    const fresh = await getJSON("/api/scenario");
    if (fresh && fresh.routing_provider) {
      state.scenario.routing_provider = fresh.routing_provider;
      if (state.trip) renderTripStatus(state.trip);
    }
  } finally {
    _routingStatusRefreshPending = false;
  }
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
    html += `<div class="status-line">Destination: <b>${escapeHtml(dest.name)}</b> ${trip.selection_type === "overridden" ? '<span class="badge warn">override</span>' : '<span class="badge ok">AI recommended</span>'}</div>`;
  }
  if (trip.override_reason) {
    
    html += `<div class="status-line">Override reason: "${escapeHtml(trip.override_reason)}"</div>`;
  }
  if (trip.status === "en_route" && trip.route_source) {
    const detail = routeSourceTechnicalDetail(trip.route_source);
    html += `<div class="status-line muted">Route: ${routeSourceSummary(trip.route_source)}${detail ? `
      <details class="route-detail">
        <summary>Technical details</summary>
        <p class="hint mono">${escapeHtml(detail)}</p>
      </details>` : ""}</div>`;
    if (trip.route_source !== "live_api") maybeRefreshRoutingProviderStatus();
  }
  if (pre) {
    html += `<div class="status-line">Pre-alert: ${pre.acknowledged_at ? '<span class="badge ok">acknowledged by hospital</span>' : '<span class="badge warn">sent, awaiting ack</span>'}</div>`;
    if (pre.handover_note) {
      html += `<div class="status-line muted">Hospital briefed: "${escapeHtml(pre.handover_note)}"</div>`;
    }
  }
  $("#tripStatus").innerHTML = html;
}

const STEP_ORDER = ["awaiting_assessment", "recommendation_ready", "en_route", "arrived"];

function renderStepper(trip) {
  const currentIndex = STEP_ORDER.indexOf(trip.status);
  $$("#tripStepper .step").forEach((el) => {
    const stepIndex = STEP_ORDER.indexOf(el.dataset.step);
    el.classList.remove("done", "active");
    if (currentIndex < 0) return;
    if (stepIndex < currentIndex) el.classList.add("done");
    else if (stepIndex === currentIndex) el.classList.add("active");
  });
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
    `<span class="icon">⚠</span> <b>Reroute suggested</b>: ${escapeHtml(trip.reroute_alert.reason)}. New recommendation: <b>${escapeHtml(suggested ? suggested.name : suggestedId)}</b>.`;
}


function contendedHospitalCounts() {
  const counts = {};
  Object.values(state.allTrips).forEach((t) => {
    if (t.status === "en_route" && t.dest_hospital_id) {
      counts[t.dest_hospital_id] = (counts[t.dest_hospital_id] || 0) + 1;
    }
  });
  return counts;
}

function conditionLabel(code) {
  if (!code || !state.scenario) return null;
  const found = state.scenario.conditions.find((c) => c.code === code);
  return found ? found.label : code;
}

function renderFleetList() {
  const el = $("#fleetList");
  const others = Object.values(state.allTrips).filter((t) => t.id !== state.myTripId);
  const counts = contendedHospitalCounts();

  const myDest = state.trip && state.trip.dest_hospital_id;
  const myContending = state.trip && state.trip.status === "en_route" && myDest && counts[myDest] > 1;
  $("#myContentionNote").classList.toggle("hidden", !myContending);

  if (others.length === 0) {
    el.innerHTML = `<p class="muted">No other ambulances active right now.</p>`;
    return;
  }
  
  el.innerHTML = others.map((t) => {
    const dest = t.dest_hospital_id ? state.hospitals[t.dest_hospital_id] : null;
    const cond = conditionLabel(t.condition_code);
    const isContending = t.status === "en_route" && t.dest_hospital_id && counts[t.dest_hospital_id] > 1;
    return `<div class="fleet-item">
      <div class="fleet-item-head">
        <span><b>${escapeHtml(t.ambulance_label)}</b>${t.autopilot ? ' <span class="icon" title="Autopilot demo ambulance">🤖</span>' : ""}</span>
        <span class="muted">${escapeHtml(statusLabel(t.status))}</span>
      </div>
      <div class="fleet-item-detail muted">
        ${cond ? escapeHtml(cond) + " · " : ""}${dest ? "→ " + escapeHtml(dest.name) : "destination not yet chosen"}
        ${isContending ? '<span class="badge contending">⚠ competing for capacity</span>' : ""}
      </div>
    </div>`;
  }).join("");
}


function conditionSeverity(code) {
  if (!state.scenario || !code) return null;
  const c = state.scenario.conditions.find((x) => x.code === code);
  return c ? c.severity : null;
}

const SEVERITY_RANK = { Red: 3, Yellow: 2, Green: 1 };

function severityRank(code) {
  return SEVERITY_RANK[conditionSeverity(code)] || 0;
}

function severityClass(code) {
  const sev = conditionSeverity(code);
  return sev ? `sev-${sev.toLowerCase()}` : "";
}

// Real seconds remaining until this trip's ambulance actually reaches its
// destination, synced to the same accelerated demo-time movement clock the
// map animation itself uses (movement_started_at/movement_duration_sec are
// both real wall-clock seconds - see app.py's _start_movement_leg) - NOT a
// literal countdown of prealert.eta_min real-world minutes, which would
// drift out of sync with what's actually happening on the map under
// DEMO_TIME_SCALE acceleration.
function remainingSeconds(trip) {
  if (!trip.movement_started_at || !trip.movement_duration_sec) return null;
  const elapsed = Date.now() / 1000 - trip.movement_started_at;
  return Math.max(0, trip.movement_duration_sec - elapsed);
}

function formatCountdown(totalSeconds) {
  const secs = Math.round(totalSeconds);
  if (secs <= 0) return "arriving now";
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function tickPrealertCountdowns() {
  $$("[data-countdown-trip]").forEach((el) => {
    const deadline = Number(el.dataset.deadline);
    if (!deadline) return;
    el.textContent = formatCountdown((deadline - Date.now()) / 1000);
  });
}

function renderHospitalView() {
  const panel = $("#prealertPanel");
  // Priority-sorted, not arrival order: clinical severity first (a Red
  // case always outranks a Yellow/Green one, regardless of who called
  // first), then soonest-arriving within the same severity tier - this is
  // "which bed do I need to free up next", not "who alerted us first".
  const alertTrips = Object.values(state.allTrips)
    .filter((t) => t.prealert)
    .sort((a, b) => {
      const sevDiff = severityRank(b.condition_code) - severityRank(a.condition_code);
      if (sevDiff !== 0) return sevDiff;
      const ra = remainingSeconds(a);
      const rb = remainingSeconds(b);
      if (ra === null && rb === null) return 0;
      if (ra === null) return 1;
      if (rb === null) return -1;
      return ra - rb;
    });

  const kpiGrid = $("#hospitalKpiGrid");
  if (kpiGrid) {
    const critical = alertTrips.filter((t) => conditionSeverity(t.condition_code) === "Red").length;
    const soon = alertTrips.filter((t) => t.prealert && t.prealert.eta_min != null && t.prealert.eta_min < 15).length;
    kpiGrid.innerHTML = `
      <div class="kpi"><div class="value">${alertTrips.length}</div><div class="label">Ambulances inbound</div></div>
      <div class="kpi"><div class="value">${critical}</div><div class="label">Critical (Red) condition</div></div>
      <div class="kpi"><div class="value">${soon}</div><div class="label">Arriving &lt; 15 min</div></div>
    `;
  }

  if (alertTrips.length === 0) {
    panel.innerHTML = `<p class="muted">No active pre-alerts yet. Select a destination hospital in the Dispatcher tab.</p>`;
  } else {
    panel.innerHTML = alertTrips.map((t, i) => {
      const pre = t.prealert;
      const h = state.hospitals[pre.hospital_id];
      const remaining = remainingSeconds(t);
      const deadlineMs = remaining !== null ? Date.now() + remaining * 1000 : null;
      const isTopPriority = i === 0 && alertTrips.length > 1;
      const isLiveHandover = pre.handover_source === "watsonx";
      return `<div class="prealert-panel active ${severityClass(t.condition_code)}">
        <h4>
          <span class="icon">🔔</span> Incoming: ${escapeHtml(t.ambulance_label)} → ${escapeHtml(h ? h.name : pre.hospital_id)}
          ${isTopPriority ? '<span class="badge bad">⏱ Next in queue</span>' : ""}
        </h4>
        <p><b>Condition:</b> ${escapeHtml(pre.condition_label || "—")} &nbsp; <b>Arriving:</b> ${
          deadlineMs !== null
            ? `<span class="countdown" data-countdown-trip="${escapeHtml(t.id)}" data-deadline="${deadlineMs}">${formatCountdown(remaining)}</span>`
            : `${Math.round(pre.eta_min)} min`
        }</p>
        <p><b>Sent at:</b> ${new Date(pre.sent_at).toLocaleTimeString()}</p>
        ${pre.handover_note ? `<div class="handover-note">
          <span class="badge ${isLiveHandover ? "ok" : "warn"}">${isLiveHandover ? "IBM watsonx.ai · Granite" : "Template fallback"}</span>
          <p>"${escapeHtml(pre.handover_note)}"</p>
        </div>` : ""}
        ${pre.acknowledged_at
          ? `<p class="badge ok">Acknowledged at ${new Date(pre.acknowledged_at).toLocaleTimeString()}</p>`
          : `<button class="btn btn-primary" data-ack="${escapeHtml(t.id)}">Acknowledge — prepare team</button>`}
      </div>`;
    }).join("");

    panel.querySelectorAll("[data-ack]").forEach((btn) => {
      btn.addEventListener("click", withBusyButton(btn, async () => {
        const tripId = btn.dataset.ack;
        await postJSON(`/api/trips/${tripId}/prealert/acknowledge`);
      }));
    });
    tickPrealertCountdowns();
  }

  const tbody = $("#hospTable tbody");
  tbody.innerHTML = "";
  Object.values(state.hospitals).forEach((h) => {
    const specs = h.specialists.filter((s) => s.on_duty).map((s) => s.type.replace("_", " ")).join(", ") || "—";
    tbody.innerHTML += `<tr>
      <td><b>${escapeHtml(h.name)}</b></td>
      <td>${capacityBar(h.ed_bays_occupied, h.ed_bays_total, "occupied")}</td>
      <td>${capacityBar(h.icu_beds_total - h.icu_beds_free, h.icu_beds_total, "free-inverted", h.icu_beds_free)}</td>
      <td>${renderCapacitySparkline(h.id)}</td>
      <td>${h.ward_beds_free}/${h.ward_beds_total}</td>
      <td>${escapeHtml(h.accepting_status)}</td>
      <td>${escapeHtml(specs)}</td>
      <td class="muted">${new Date(h.last_capacity_update_at).toLocaleTimeString()}</td>
    </tr>`;
  });
}


function recordCapacitySnapshot(hospitals) {
  // Called every time state.hospitals is (re)assigned - from the initial
  // fetch, every "hospitals_updated" websocket push (manual capacity edits
  // AND real bed reservation/release on selection or cancellation both go
  // through this), and demo reset. This is the only place capacity history
  // is captured, so the Hospital View's ICU trend sparkline reflects every
  // real change, not just the manually-edited "simulate a live change"
  // scenarios.
  const now = Date.now();
  Object.values(hospitals || {}).forEach((h) => {
    const hist = state.capacityHistory[h.id] || (state.capacityHistory[h.id] = []);
    const last = hist[hist.length - 1];
    if (last && last.icu_free === h.icu_beds_free && last.ed_occupied === h.ed_bays_occupied) {
      return; // unchanged since the last recorded point - don't clutter the line with flat duplicates
    }
    hist.push({
      t: now,
      icu_free: h.icu_beds_free,
      icu_total: h.icu_beds_total,
      ed_occupied: h.ed_bays_occupied,
      ed_total: h.ed_bays_total,
    });
    if (hist.length > CAPACITY_HISTORY_MAX_POINTS) hist.shift();
  });
}

function renderCapacitySparkline(hospitalId) {
  const hist = state.capacityHistory[hospitalId];
  if (!hist || hist.length < 2) {
    return `<span class="muted spark-empty">no history yet</span>`;
  }
  const total = hist[hist.length - 1].icu_total || 1;
  const w = 84, h = 22, pad = 2;
  const points = hist.map((p, i) => {
    const x = pad + (hist.length === 1 ? 0 : (i / (hist.length - 1)) * (w - pad * 2));
    const frac = total > 0 ? Math.max(0, Math.min(1, p.icu_free / total)) : 0;
    const y = h - pad - frac * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const last = hist[hist.length - 1];
  const trendCls = last.icu_free === 0 ? "crit" : (last.icu_free <= Math.max(1, Math.round(total * 0.2)) ? "warn" : "ok");
  return `<svg class="spark spark-${trendCls}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="ICU-free beds trend, currently ${last.icu_free} of ${total}">
    <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" />
  </svg>`;
}

function capacityBar(usedCount, total, mode, freeCount) {
  if (!total) return `<span class="muted">n/a</span>`;
  const fraction = mode === "free-inverted" ? (total - freeCount) / total : usedCount / total;
  const pct = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  let cls = "ok";
  if (mode === "free-inverted") {
    if (freeCount === 0) cls = "crit";
    else if (freeCount <= Math.max(1, Math.round(total * 0.2))) cls = "warn";
  } else {
    if (pct >= 95) cls = "crit";
    else if (pct >= 75) cls = "warn";
  }
  const label = mode === "free-inverted" ? `${freeCount}/${total} free` : `${usedCount}/${total}`;
  return `<div class="cap-bar-wrap"><div class="cap-bar-track"><div class="cap-bar-fill ${cls}" style="width:${pct}%"></div></div><span class="cap-bar-text">${label}</span></div>`;
}


const EVENT_LABELS = {
  trip_created: "Ambulance dispatched",
  trip_cancelled: "Trip cancelled",
  recommendation_generated: "Recommendation generated",
  hospital_selected: "Hospital selected",
  prealert_sent: "Pre-alert sent",
  prealert_acknowledged: "Pre-alert acknowledged",
  reroute_suggested: "Reroute suggested",
  reroute_accepted: "Reroute accepted",
  reroute_declined: "Reroute declined",
  arrived: "Arrived at hospital",
  capacity_update: "Hospital capacity changed",
};

function hospitalName(id) {
  if (!id) return "—";
  return state.hospitals[id] ? state.hospitals[id].name : id;
}


function tripLabel(tripId) {
  if (!tripId) return "Unknown ambulance";
  if (state.allTrips[tripId]) return state.allTrips[tripId].ambulance_label;
  if (tripId === state.myTripId && state.trip) return state.trip.ambulance_label;
  return `Trip ${tripId.slice(0, 8)}`;
}

function formatCapacityChange(changed) {
  if (!changed || Object.keys(changed).length === 0) return "no fields changed";
  const FIELD_LABELS = { icu_beds_free: "ICU beds free", ed_bays_occupied: "ED bays occupied", accepting_status: "accepting status" };
  return Object.entries(changed).map(([k, v]) => {
    if (k.startsWith("specialist:")) {
      const type = k.slice("specialist:".length).replace(/_/g, " ");
      return `${type} ${v ? "on duty" : "off duty"}`;
    }
    return `${FIELD_LABELS[k] || k} → ${v}`;
  }).join(", ");
}


function describeAuditEvent(e) {
  const p = e.payload || {};
  const amb = escapeHtml(tripLabel(p.trip_id));
  switch (e.event_type) {
    case "trip_created":
      return `${escapeHtml(p.ambulance_label) || amb} dispatched${p.autopilot ? " — autopilot demo ambulance" : ""}.`;
    case "trip_cancelled":
      return `${amb} cancelled.`;
    case "recommendation_generated":
      return `${amb}: recommended ${escapeHtml(hospitalName(p.recommended_hospital_id))} for ${escapeHtml(p.condition_code)}.`;
    case "hospital_selected":
      return p.selection_type === "overridden"
        ? `${amb} overrode the recommendation and chose ${escapeHtml(hospitalName(p.hospital_id))}${p.override_reason ? ` — "${escapeHtml(p.override_reason)}"` : ""}.`
        : `${amb} accepted the recommended hospital: ${escapeHtml(hospitalName(p.hospital_id))}.`;
    case "prealert_sent":
      return `Pre-alert sent to ${escapeHtml(hospitalName(p.hospital_id))} for ${amb} — ETA ${Math.round(p.eta_min)} min.`;
    case "prealert_acknowledged":
      return `${escapeHtml(hospitalName(p.hospital_id))} acknowledged the pre-alert for ${amb}.`;
    case "reroute_suggested":
      return `Reroute suggested for ${amb}: ${escapeHtml(p.reason)} — new suggestion ${escapeHtml(hospitalName(p.suggested_hospital_id))}.`;
    case "reroute_accepted":
      return `${amb} rerouted to ${escapeHtml(hospitalName(p.new_hospital_id))}.`;
    case "reroute_declined":
      return `${amb} kept its current destination (declined reroute${p.reason ? `: ${escapeHtml(p.reason)}` : ""}).`;
    case "arrived":
      return `${amb} arrived at ${escapeHtml(hospitalName(p.hospital_id))}.`;
    case "capacity_update":
      return `${escapeHtml(hospitalName(p.hospital_id))}: ${escapeHtml(formatCapacityChange(p.changed))}.`;
    default:
      
      return `${escapeHtml(e.event_type)}: ${escapeHtml(JSON.stringify(p))}`;
  }
}

function computeHospitalLeaderboard(audit) {
  const recommended = {};
  const selected = {};
  audit.forEach((e) => {
    const p = e.payload || {};
    if (e.event_type === "recommendation_generated" && p.recommended_hospital_id) {
      recommended[p.recommended_hospital_id] = (recommended[p.recommended_hospital_id] || 0) + 1;
    } else if (e.event_type === "hospital_selected" && p.hospital_id) {
      selected[p.hospital_id] = (selected[p.hospital_id] || 0) + 1;
    }
  });
  // Include every hospital that exists, even one that's never been
  // recommended or selected yet - a zero-activity row is itself useful
  // information ("this one has never come up"), not noise to hide.
  const ids = new Set([...Object.keys(state.hospitals), ...Object.keys(recommended), ...Object.keys(selected)]);
  return Array.from(ids)
    .map((id) => ({ id, name: hospitalName(id), recommended: recommended[id] || 0, selected: selected[id] || 0 }))
    .sort((a, b) => (b.recommended + b.selected) - (a.recommended + a.selected));
}

function renderHospitalLeaderboard(rows) {
  const el = $("#hospitalLeaderboard");
  if (!el) return;
  if (rows.length === 0) {
    el.innerHTML = `<p class="muted">No cases yet.</p>`;
    return;
  }
  const max = Math.max(1, ...rows.map((r) => Math.max(r.recommended, r.selected)));
  el.innerHTML = `
    <div class="leaderboard-legend">
      <span><i class="legend-swatch legend-rec"></i>Recommended by AI</span>
      <span><i class="legend-swatch legend-sel"></i>Actually selected</span>
    </div>
    ${rows.map((r) => `
      <div class="leaderboard-row">
        <div class="leaderboard-name" title="${escapeHtml(r.name)}">${escapeHtml(r.name)}</div>
        <div class="leaderboard-bars">
          <div class="leaderboard-bar-track"><div class="leaderboard-bar rec" style="width:${Math.max(2, (r.recommended / max) * 100)}%"></div></div>
          <div class="leaderboard-bar-track"><div class="leaderboard-bar sel" style="width:${Math.max(2, (r.selected / max) * 100)}%"></div></div>
        </div>
        <div class="leaderboard-counts muted">${r.recommended} / ${r.selected}</div>
      </div>
    `).join("")}
  `;
}

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
  renderHospitalLeaderboard(computeHospitalLeaderboard(audit));
  const tbody = $("#auditTable tbody");
  tbody.innerHTML = "";
  
  audit.slice().reverse().forEach((e) => {
    tbody.innerHTML += `<tr>
      <td class="muted">${new Date(e.created_at).toLocaleTimeString()}</td>
      <td><span class="badge audit-cat">${escapeHtml(EVENT_LABELS[e.event_type] || e.event_type)}</span></td>
      <td>${describeAuditEvent(e)}</td>
    </tr>`;
  });
}

function setTriageProviderLine(triageProvider) {
  const el = $("#triageProviderLine");
  if (!el || !triageProvider) return;
  el.textContent = triageProvider.active_mode === "watsonx"
    ? `Powered by IBM watsonx.ai (${triageProvider.model_id}).`
    : "Fallback mode: labeled keyword classifier (no watsonx.ai credentials configured).";
}

function renderTriageSuggestion(suggestion) {
  const el = $("#triageResult");
  if (!el) return;
  const condition = (state.scenario?.conditions || []).find((c) => c.code === suggestion.condition_code);
  const label = condition ? condition.label : suggestion.condition_code;
  const isLive = suggestion.source === "watsonx";

  el.className = isLive ? "triage-result" : "triage-result fallback";
  el.hidden = false;
  el.innerHTML = `
    <div class="triage-result-head">
      <span class="badge ${isLive ? "ok" : "warn"}">${isLive ? `IBM watsonx.ai · ${escapeHtml(suggestion.model_id || "Granite")}` : "Keyword fallback"}</span>
      <span class="muted">confidence ${Math.round(suggestion.confidence * 100)}%</span>
    </div>
    <div class="triage-result-suggestion">Suggested: <b>${escapeHtml(label)}</b></div>
    <div class="triage-result-reason">${escapeHtml(suggestion.reasoning)}</div>
  `;
}

function populateSelects(conditions, hospitals) {
  const condSel = $("#conditionSelect");
  
  condSel.innerHTML =
    `<option value="" disabled selected>— Select a condition —</option>` +
    conditions.map((c) => `<option value="${escapeHtml(c.code)}">${escapeHtml(c.label)} (${escapeHtml(c.severity)})</option>`).join("");

  const simSel = $("#simHospital");
  simSel.innerHTML = Object.values(hospitals).map((h) => `<option value="${escapeHtml(h.id)}">${escapeHtml(h.name)}</option>`).join("");
}


function syncSimHospitalDefault(trip) {
  const sel = $("#simHospital");
  if (!sel || !trip || !trip.dest_hospital_id) return;
  if (state.simHospitalUserPicked) return;
  if (sel.value === trip.dest_hospital_id) return;
  if (!sel.querySelector(`option[value="${CSS.escape(trip.dest_hospital_id)}"]`)) return;
  sel.value = trip.dest_hospital_id;
}

function setupControls() {
  const assessBtn = $("#assessBtn");
  
  $("#conditionSelect").addEventListener("change", (e) => {
    assessBtn.disabled = !e.target.value;
  });

  const triageBtn = $("#triageSuggestBtn");
  if (triageBtn) {
    triageBtn.addEventListener("click", withBusyButton(triageBtn, async () => {
      const noteInput = $("#triageNoteInput");
      const note = (noteInput.value || "").trim();
      if (!note) {
        toast("Type a dispatcher note first, e.g. the symptoms the caller described.", "error");
        return;
      }
      const suggestion = await postJSON(`/api/trips/${state.myTripId}/triage`, { note });
      if (suggestion) {
        renderTriageSuggestion(suggestion);
        setTriageProviderLine({ active_mode: suggestion.source === "watsonx" ? "watsonx" : "keyword_fallback", model_id: suggestion.model_id });
        $("#conditionSelect").value = suggestion.condition_code;
        assessBtn.disabled = !suggestion.condition_code;
      }
    }));
  }
  assessBtn.addEventListener("click", withBusyButton(assessBtn, async () => {
    const code = $("#conditionSelect").value;
    if (!code) return;
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

  
  document.querySelectorAll(".scenario-btn").forEach((btn) => {
    btn.addEventListener("click", withBusyButton(btn, async () => {
      const hospitalId = $("#simHospital").value;
      const scenario = btn.dataset.scenario;
      const h = state.hospitals[hospitalId];
      let body = {};
      if (scenario === "icu-zero") {
        body = { icu_beds_free: 0 };
      } else if (scenario === "ed-overload") {
        body = { ed_bays_occupied: h ? h.ed_bays_total : undefined };
      } else if (scenario === "cardiologist-on") {
        body = { specialist_type: "cardiologist", specialist_on_duty: true };
      } else if (scenario === "cardiologist-off") {
        body = { specialist_type: "cardiologist", specialist_on_duty: false };
      }
      const result = await postJSON(`/api/hospitals/${hospitalId}/capacity`, body);
      if (result) {
        toast("Scenario applied.", "success");
        
        const demoControls = $("#demoControls");
        if (demoControls) demoControls.open = false;
      }
    }));
  });

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

  
  $("#simHospital").addEventListener("change", () => {
    state.simHospitalUserPicked = true;
  });

  $("#resetBtn").addEventListener("click", withBusyButton($("#resetBtn"), async () => {
    Object.keys(state.ambulanceMarkers).forEach(removeAmbulanceMarker);
    Object.keys(state.routeLines).forEach(removeRouteLine);
    state.allTrips = {};
    state.simHospitalUserPicked = false;
    $("#conditionSelect").value = "";
    $("#assessBtn").disabled = true;
    if ($("#triageNoteInput")) $("#triageNoteInput").value = "";
    if ($("#triageResult")) $("#triageResult").hidden = true;
    state.capacityHistory = {};
    await postJSON("/api/reset");
    await startMyTrip();
    const hospitals = await getJSON("/api/hospitals");
    if (hospitals) {
      state.hospitals = Object.fromEntries(hospitals.map((h) => [h.id, h]));
      recordCapacitySnapshot(state.hospitals);
      renderHospitalsOnMap(state.hospitals);
    }
    renderAll();
    toast("Demo reset.", "success");
  }));
}

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
      if (msg.trip.status === "en_route" && msg.trip.route_points) {
        upsertRouteLine(msg.trip_id, msg.trip.route_points, isMine);
      } else {
        removeRouteLine(msg.trip_id);
      }
    }

    if (msg.type === "trip_removed") {
      removeAmbulanceMarker(msg.trip_id);
      removeRouteLine(msg.trip_id);
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
      recordCapacitySnapshot(state.hospitals);
      renderHospitalsOnMap(state.hospitals);
      renderAll();
    }
  };
  state.ws.onclose = (ev) => {
    
    if (ev && ev.code === 4401) {
      setConnectionStatus("offline");
      setToken("");
      toast("Access token rejected — please re-enter it.", "error");
      showAccessGate("Session expired or access token changed. Please re-enter it.").then(() => connectWs());
      return;
    }
    setConnectionStatus("reconnecting");
    setTimeout(connectWs, 1500);
  };
  state.ws.onerror = () => toast("Lost connection to backend — retrying...", "error");
  state.ws.onopen = () => setConnectionStatus("live");
  setInterval(() => {
    if (state.ws.readyState === WebSocket.OPEN) state.ws.send("ping");
  }, 15000);
}

function renderAll() {
  if (!state.trip) return;
  renderStepper(state.trip);
  renderAssessCard(state.trip);
  renderRecommendation(state.trip);
  renderTripStatus(state.trip);
  renderRerouteBanner(state.trip);
  renderFleetList();
  syncSimHospitalDefault(state.trip);
  if ($("#tab-hospital").classList.contains("active")) renderHospitalView();
}

async function startMyTrip() {
  const trip = await postJSON("/api/trips", {});
  if (!trip) return;
  state.myTripId = trip.id;
  state.trip = trip;
  $("#myAmbulanceLabel").textContent = `(${trip.ambulance_label})`;
  upsertAmbulanceMarker(trip.id, trip.ambulance_pos, trip.ambulance_label, true);
}


function tickClock() {
  const el = document.getElementById("liveClock");
  if (!el) return;
  el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function init() {
  setupTabs();
  setupControls();
  setupAccessGate();
  tickClock();
  setInterval(() => { tickClock(); tickPrealertCountdowns(); }, 1000);

  const scenario = await getJSON("/api/scenario");
  if (!scenario) {
    toast("Could not reach the backend. Is uvicorn running?", "error");
    return;
  }
  state.scenario = scenario;
  $("#etaProviderBadge").textContent = `ETA source: ${scenario.eta_provider.active_mode === "live_api" ? "live traffic API" : "simulated (no API key configured)"}`;
  setTriageProviderLine(scenario.triage_provider);

  const hospitalsList = await getJSON("/api/hospitals");
  state.hospitals = Object.fromEntries((hospitalsList || []).map((h) => [h.id, h]));
  recordCapacitySnapshot(state.hospitals);

  try {
    initMap(scenario.incident_location);
    renderHospitalsOnMap(state.hospitals);
    $("#mapLegend").classList.remove("hidden");
  } catch (e) {
    
    console.error("Map initialization failed - continuing without the map:", e);
    toast("Map could not load (network/CDN issue). The rest of the app will still work.", "error");
    const mapEl = $("#map");
    if (mapEl) {
      mapEl.innerHTML =
        '<div class="map-fallback"><span class="icon">🗺️</span>' +
        "<div><b>Map unavailable</b></div>" +
        "<div>The map tiles couldn't be loaded (network or CDN issue). " +
        "Dispatch, recommendations, and rerouting below still work normally.</div></div>";
    }
  }
  populateSelects(scenario.conditions, state.hospitals);

  await startMyTrip();
  renderAll();
  connectWs();
}

init();
