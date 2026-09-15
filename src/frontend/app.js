// AURA Dispatch

const state = {
  map: null,
  tileLayer: null,
  theme: localStorage.getItem("theme") || "light",
  soundMuted: localStorage.getItem("soundMuted") === "true",
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
};

const BACKEND_ORIGIN = (window.COORDINATOR_BACKEND_URL || "").replace(/\/$/, "");

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ------------------------------------------------------------- Web Audio Alert System ----
let audioCtx = null;
function getAudioContext() {
  if (!audioCtx) {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (AudioContext) audioCtx = new AudioContext();
  }
  if (audioCtx && audioCtx.state === "suspended") {
    audioCtx.resume();
  }
  return audioCtx;
}

function playAlertSound(type = "info") {
  if (state.soundMuted) return;
  try {
    const ctx = getAudioContext();
    if (!ctx) return;

    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);

    const now = ctx.currentTime;
    if (type === "reroute" || type === "error") {
      osc.type = "sawtooth";
      osc.frequency.setValueAtTime(880, now);
      osc.frequency.setValueAtTime(660, now + 0.15);
      gain.gain.setValueAtTime(0.15, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
      osc.start(now);
      osc.stop(now + 0.4);
    } else if (type === "prealert" || type === "success") {
      osc.type = "sine";
      osc.frequency.setValueAtTime(523.25, now);
      osc.frequency.setValueAtTime(659.25, now + 0.12);
      gain.gain.setValueAtTime(0.12, now);
      gain.gain.exponentialRampToValueAtTime(0.01, now + 0.35);
      osc.start(now);
      osc.stop(now + 0.35);
    }
  } catch (e) {
    console.warn("Audio play error:", e);
  }
}

// ------------------------------------------------------------- Header & Mobile Menu ----
function setupNavigation() {
  const navbar = $("#navbar");
  if (navbar) {
    window.addEventListener("scroll", () => {
      if (window.scrollY > 20) {
        navbar.style.boxShadow = "0 10px 30px rgba(15, 23, 42, 0.1)";
      } else {
        navbar.style.boxShadow = "none";
      }
    });
  }

  const hamburger = $("#hamburgerBtn");
  const navLinks = $("#navLinks");
  if (hamburger && navLinks) {
    hamburger.addEventListener("click", () => {
      const isVisible = navLinks.style.display === "flex";
      navLinks.style.display = isVisible ? "none" : "flex";
      navLinks.style.flexDirection = "column";
      navLinks.style.position = "absolute";
      navLinks.style.top = "60px";
      navLinks.style.left = "0";
      navLinks.style.right = "0";
      navLinks.style.background = "var(--header-bg)";
      navLinks.style.padding = "20px";
    });
  }
}

// ------------------------------------------------------------- Animated Statistics Counter ----
function setupAnimatedCounters() {
  const statNumbers = $$(".stat-number[data-target]");
  if (!statNumbers.length) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        const el = entry.target;
        const target = parseFloat(el.dataset.target);
        const duration = 2000;
        const startTime = performance.now();

        function updateCounter(currentTime) {
          const elapsed = currentTime - startTime;
          const progress = Math.min(elapsed / duration, 1);
          const currentVal = (progress * target).toFixed(target % 1 !== 0 ? 1 : 0);
          el.textContent = currentVal;
          if (progress < 1) {
            requestAnimationFrame(updateCounter);
          } else {
            el.textContent = target;
          }
        }

        requestAnimationFrame(updateCounter);
        observer.unobserve(el);
      }
    });
  }, { threshold: 0.5 });

  statNumbers.forEach((num) => observer.observe(num));
}

// ------------------------------------------------------------- Theme Engine ----
function initTheme() {
  setTheme(state.theme);
  const themeBtn = $("#themeToggleBtn");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const nextTheme = state.theme === "dark" ? "light" : "dark";
      setTheme(nextTheme);
    });
  }

  const soundBtn = $("#soundToggleBtn");
  if (soundBtn) {
    updateSoundButtonUI();
    soundBtn.addEventListener("click", () => {
      state.soundMuted = !state.soundMuted;
      localStorage.setItem("soundMuted", String(state.soundMuted));
      updateSoundButtonUI();
      if (!state.soundMuted) playAlertSound("success");
    });
  }
}

function updateSoundButtonUI() {
  const onIcon = $(".sound-on");
  const offIcon = $(".sound-off");
  if (onIcon && offIcon) {
    onIcon.classList.toggle("hidden", state.soundMuted);
    offIcon.classList.toggle("hidden", !state.soundMuted);
  }
}

function setTheme(theme) {
  state.theme = theme;
  localStorage.setItem("theme", theme);
  document.documentElement.setAttribute("data-theme", theme);
  document.body.className = `theme-${theme}`;

  if (state.map) {
    if (state.tileLayer) state.map.removeLayer(state.tileLayer);
    
    const tileUrl = theme === "dark"
      ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
      : "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png";

    state.tileLayer = L.tileLayer(tileUrl, {
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
      maxZoom: 19
    }).addTo(state.map);
  }
}

// ------------------------------------------------------------- Safe HTML ----
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
  try { return localStorage.getItem("accessToken") || ""; } catch { return ""; }
}
function setToken(token) {
  try { localStorage.setItem("accessToken", token); } catch {}
}
function authHeaders() {
  const token = getToken();
  return token ? { "X-API-Key": token } : {};
}

// --------------------------------------------------------------- Toasts ----
const TOAST_ICON = { info: "ℹ️", success: "✅", error: "🚨" };

function toast(message, kind = "info") {
  const stack = $("#toastStack");
  if (!stack) return;
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

  if (kind === "error") playAlertSound("error");
  else if (kind === "success") playAlertSound("success");

  setTimeout(() => el.remove(), 5000);
}

// ---------------------------------------------------------- Fetch helpers ----
async function request(url, options = {}) {
  let r;
  try {
    const requestUrl = url.startsWith("http") ? url : `${BACKEND_ORIGIN}${url}`;
    r = await fetch(requestUrl, {
      ...options,
      headers: { ...(options.headers || {}), ...authHeaders() },
    });
  } catch (e) {
    toast(`Backend Connection Error: ${e.message}`, "error");
    return null;
  }

  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    toast(`Request failed: ${body.detail || r.statusText}`, "error");
    return null;
  }
  return await r.json();
}

async function getJSON(url) { return request(url); }
async function postJSON(url, body) {
  return request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function withBusyButton(btn, handler) {
  return async (...args) => {
    if (btn.disabled) return;
    const original = btn.innerHTML;
    btn.disabled = true;
    try {
      await handler(...args);
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  };
}

// ----------------------------------------------------------------- Map ----
function initMap(incident) {
  const mapEl = $("#map");
  if (!mapEl) return;
  state.map = L.map("map", { zoomControl: true }).setView([incident.lat, incident.lng], 13);
  
  setTheme(state.theme);

  const incidentIcon = L.divIcon({
    className: "incident-map-marker",
    html: `<div style="font-size:26px; filter: drop-shadow(0 0 12px rgba(244,63,94,0.9));">📍</div>`,
    iconSize: [30, 30],
    iconAnchor: [15, 26],
  });

  state.incidentMarker = L.marker([incident.lat, incident.lng], { icon: incidentIcon })
    .addTo(state.map)
    .bindPopup("<b>Incident Scene Location</b><br/>Primary Unit Origin");
}

function ambulanceIcon(isMine, label) {
  const color = isMine ? "#6366F1" : "#64748b";
  const glow = isMine ? "box-shadow: 0 0 15px #6366F1, 0 0 5px #22C55E;" : "";
  return L.divIcon({
    className: "custom-amb-marker",
    html: `<div style="background:${color}; color:#fff; padding:5px 10px; border-radius:14px; font-weight:800; font-size:11px; display:flex; align-items:center; gap:5px; ${glow}">
      <span style="font-size:14px;">🚑</span> <span>${escapeHtml(label || "Unit")}</span>
    </div>`,
    iconSize: [95, 28],
    iconAnchor: [47, 14],
  });
}

function upsertAmbulanceMarker(tripId, pos, label, isMine) {
  if (!pos || !state.map) return;
  if (state.ambulanceMarkers[tripId]) {
    state.ambulanceMarkers[tripId].setLatLng([pos.lat, pos.lng]);
  } else {
    const marker = L.marker([pos.lat, pos.lng], { icon: ambulanceIcon(isMine, label) })
      .addTo(state.map)
      .bindPopup(`<b>Unit: ${escapeHtml(label)}</b><br/>Status: Dispatch Active`);
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
      color: isMine ? "#6366F1" : "#64748b",
      weight: isMine ? 6 : 3,
      opacity: 0.9,
      dashArray: isMine ? null : "4 8",
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
  if (h.accepting_status === "no" || h.icu_beds_free === 0) return "#EF4444";
  if (h.accepting_status === "limited") return "#F59E0B";
  return "#22C55E";
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
        radius: 12,
        color,
        fillColor: color,
        fillOpacity: 0.9,
        weight: 3,
      })
        .addTo(state.map)
        .bindPopup(hospitalPopupHtml(h));
      state.hospitalMarkers[h.id] = marker;
    }
  });
}

function hospitalPopupHtml(h) {
  const specs = h.specialists.filter((s) => s.on_duty).map((s) => s.type).join(", ") || "None on Duty";
  return `<div style="font-family:'Inter', sans-serif;">
    <b style="font-size:14px; font-family:'Manrope',sans-serif;">🏥 ${escapeHtml(h.name)}</b><br/>
    <div style="margin-top:6px; font-size:12px; line-height:1.45;">
      <b>ICU Free:</b> ${h.icu_beds_free} / ${h.icu_beds_total}<br/>
      <b>ED Occupied:</b> ${h.ed_bays_occupied} / ${h.ed_bays_total}<br/>
      <b>Status:</b> <span class="badge ${h.accepting_status === 'yes' ? 'ok' : 'bad'}">${escapeHtml(h.accepting_status)}</span><br/>
      <b>Specialists:</b> ${escapeHtml(specs)}
    </div>
  </div>`;
}

// --------------------------------------------------------- Recommendation ----
function statusCellHtml(c, preferredSpecialist) {
  const rows = [];
  rows.push(c.icu_available
    ? `<span class="badge ok">ICU READY</span>`
    : `<span class="badge bad">ICU FULL</span>`);
  if (preferredSpecialist) {
    rows.push(c.specialist_match
      ? `<span class="badge ok">SPEC MATCH</span>`
      : `<span class="badge bad">NO SPEC</span>`);
  }
  const loadLabel = c.ed_load_state === "overloaded" ? "ED FULL" : c.ed_load_state === "busy" ? "ED BUSY" : "ED NORMAL";
  const loadCls = c.ed_load_state === "overloaded" ? "bad" : c.ed_load_state === "busy" ? "warn" : "ok";
  rows.push(`<span class="badge ${loadCls}">${loadLabel}</span>`);
  return rows.join(" ");
}

function renderRecHero(rec) {
  const hero = $("#recHero");
  if (!hero) return;
  const top = rec.candidates.find((c) => c.hospital_id === rec.recommended_hospital_id);
  if (!top) {
    hero.innerHTML = `<div class="rec-hero"><div class="rec-hero-name">No viable hospital found for this clinical state.</div></div>`;
    return;
  }
  hero.innerHTML = `
    <div class="rec-hero">
      <div class="rec-hero-label"><span class="icon">★</span> AI TOP RANKED FACILITY — FASTEST TREATMENT</div>
      <div class="rec-hero-name">🏥 ${escapeHtml(top.name)}</div>
      <div class="rec-hero-stats">
        <div class="rec-hero-stat">Drive ETA<b>${top.eta_min != null ? Math.round(top.eta_min) + " min" : "—"}</b></div>
        <div class="rec-hero-stat">Total Delay<b>${top.viable ? Math.round(top.est_total_treatment_delay_min) + " min" : "—"}</b></div>
        <div class="rec-hero-stat">Distance<b>${top.distance_km != null ? top.distance_km.toFixed(1) + " km" : "—"}</b></div>
        <div class="rec-hero-stat">ICU Status<b>${top.icu_available ? "Available" : "Full"}</b></div>
      </div>
      <div class="rec-hero-reasons">${top.reasons.map(escapeHtml).join(" • ")}</div>
    </div>
  `;
}

function renderRecommendation(trip) {
  const rec = trip.recommendation;
  const recCard = $("#recommendationCard");
  if (!recCard) return;
  if (!rec || trip.status !== "recommendation_ready") {
    recCard.hidden = true;
    return;
  }
  recCard.hidden = false;
  $("#conditionLabel").textContent = `— ${rec.condition_label}`;

  const hudCond = $("#hudConditionVal");
  if (hudCond) hudCond.textContent = rec.condition_label;

  renderRecHero(rec);

  const tbody = $("#recTable tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  rec.candidates.forEach((c) => {
    const isRecommended = c.hospital_id === rec.recommended_hospital_id;
    const tr = document.createElement("tr");
    tr.className = (isRecommended ? "recommended " : "") + (!c.viable ? "not-viable" : "");

    const safeId = escapeHtml(c.hospital_id);
    const actionCell = trip.status === "en_route" || trip.status === "arrived"
      ? ""
      : `<button class="btn btn-small ${isRecommended ? "btn-primary pill-btn" : "btn-ghost pill-btn"}" data-select="${safeId}">
           ${isRecommended ? "Select Top Pick" : "Choose"}
         </button>`;

    tr.innerHTML = `
      <td data-label="Hospital">
        <b>${escapeHtml(c.name)}</b>${isRecommended ? '<span class="badge star">★ TOP MATCH</span>' : ""}
        <div class="muted">${c.distance_km != null ? c.distance_km.toFixed(1) + " km away" : ""}</div>
      </td>
      <td data-label="Delay">
        <div style="font-weight:700; font-family:'Space Mono',monospace;">${c.viable ? Math.round(c.est_total_treatment_delay_min) + "m delay" : "N/A"}</div>
        <div class="muted">${c.eta_min != null ? Math.round(c.eta_min) + "m drive" : "—"}</div>
      </td>
      <td data-label="Status">${statusCellHtml(c, rec.preferred_specialist)}</td>
      <td data-label="">${actionCell}</td>
    `;
    tbody.appendChild(tr);
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
  const card = $("#overrideCard");
  if (card) {
    card.hidden = false;
    $("#overrideReasonInput").value = "";
    $("#overrideReasonInput").focus();
  }
}

function closeOverrideCard() {
  state.pendingOverrideHospitalId = null;
  const card = $("#overrideCard");
  if (card) card.hidden = true;
}

async function selectHospital(hospitalId, overrideReason) {
  const trip = await postJSON(`/api/trips/${state.myTripId}/select`, {
    hospital_id: hospitalId,
    override_reason: overrideReason,
  });
  if (trip) {
    state.trip = trip;
    renderAll();
    playAlertSound("success");
  }
}

function renderAssessCard(trip) {
  const card = $("#assessCard");
  if (card) card.hidden = trip.status !== "awaiting_assessment";
}

function statusLabel(s) {
  return {
    awaiting_assessment: "Awaiting Triage Assessment",
    recommendation_ready: "Recommendation Ready — Select Destination",
    en_route: "En Route to Destination",
    arrived: "Arrived at ER Department",
  }[s] || s;
}

function renderTripStatus(trip) {
  const card = $("#tripCard");
  if (!card) return;
  if (trip.status === "awaiting_assessment") {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const dest = trip.dest_hospital_id ? state.hospitals[trip.dest_hospital_id] : null;

  let html = `<div style="font-size:13px; margin-bottom:8px;"><b>Status:</b> ${statusLabel(trip.status)}</div>`;
  if (dest) {
    html += `<div style="font-size:13px; margin-bottom:8px;"><b>Target Hospital:</b> <span style="font-weight:800; color:var(--accent-primary);">${escapeHtml(dest.name)}</span> ${trip.selection_type === "overridden" ? '<span class="badge warn">OVERRIDE CHOICE</span>' : '<span class="badge ok">AI TOP MATCH</span>'}</div>`;
  }
  if (trip.override_reason) {
    html += `<div class="muted">Override Rationale: "${escapeHtml(trip.override_reason)}"</div>`;
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
  if (!banner) return;
  if (!trip.reroute_alert) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  playAlertSound("reroute");
  const suggestedId = trip.reroute_alert.snapshot.recommended_hospital_id;
  const suggested = state.hospitals[suggestedId];
  banner.querySelector(".reroute-text").innerHTML =
    `Dynamic capacity change: <b>${escapeHtml(trip.reroute_alert.reason)}</b>. Suggested new target: <b>${escapeHtml(suggested ? suggested.name : suggestedId)}</b>.`;
}

function renderFleetList() {
  const el = $("#fleetList");
  if (!el) return;
  const others = Object.values(state.allTrips).filter((t) => t.id !== state.myTripId);

  if (others.length === 0) {
    el.innerHTML = `<p class="muted">No other active units in fleet.</p>`;
    return;
  }
  el.innerHTML = others.map((t) => {
    const dest = t.dest_hospital_id ? state.hospitals[t.dest_hospital_id] : null;
    return `<div style="background:var(--bg-primary); border:1px solid var(--border-color); padding:10px 12px; border-radius:10px; margin-top:8px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <b>🚑 ${escapeHtml(t.ambulance_label)}</b>
        <span class="muted" style="font-size:11px;">${escapeHtml(statusLabel(t.status))}</span>
      </div>
      <div class="muted" style="margin-top:4px; font-size:11px;">Destination: ${dest ? escapeHtml(dest.name) : "Unassigned"}</div>
    </div>`;
  }).join("");
}

function populateSelects(conditions, hospitals) {
  const condSel = $("#conditionSelect");
  if (!condSel) return;
  condSel.innerHTML =
    `<option value="" disabled selected>— Select Clinical Presentation —</option>` +
    conditions.map((c) => `<option value="${escapeHtml(c.code)}">${escapeHtml(c.label)} (${escapeHtml(c.severity)})</option>`).join("");

  const quickGrid = $("#quickConditionPills");
  if (quickGrid) {
    const icons = { CARDIAC_ARREST: "❤️", STROKE_ACUTE: "🧠", TRAUMA_MAJOR: "🩹", RESPIRATORY_DISTRESS: "🫁" };
    quickGrid.innerHTML = conditions.slice(0, 4).map((c) => `
      <div class="condition-card-btn" data-code="${escapeHtml(c.code)}">
        <span class="c-btn-icon">${icons[c.code] || "⚡"}</span>
        <div>
          <div class="c-btn-title">${escapeHtml(c.label)}</div>
          <div class="c-btn-sev">${escapeHtml(c.severity)} Priority</div>
        </div>
      </div>
    `).join("");

    quickGrid.querySelectorAll(".condition-card-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        condSel.value = btn.dataset.code;
        $("#assessBtn").disabled = false;
        playAlertSound("info");
      });
    });
  }

  const simSel = $("#simHospital");
  if (simSel) {
    simSel.innerHTML = Object.values(hospitals).map((h) => `<option value="${escapeHtml(h.id)}">${escapeHtml(h.name)}</option>`).join("");
  }
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
  const condSel = $("#conditionSelect");
  if (condSel && assessBtn) {
    condSel.addEventListener("change", (e) => {
      assessBtn.disabled = !e.target.value;
    });
    assessBtn.addEventListener("click", withBusyButton(assessBtn, async () => {
      const code = condSel.value;
      if (!code) return;
      const trip = await postJSON(`/api/trips/${state.myTripId}/case`, { condition_code: code });
      if (trip) {
        state.trip = trip;
        renderAll();
        playAlertSound("prealert");
      }
    }));
  }

  const confirmBtn = $("#overrideConfirmBtn");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", async () => {
      const reason = $("#overrideReasonInput").value.trim() || "Crew rationale";
      const hospitalId = state.pendingOverrideHospitalId;
      closeOverrideCard();
      await selectHospital(hospitalId, reason);
    });
  }

  const cancelBtn = $("#overrideCancelBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", closeOverrideCard);

  document.querySelectorAll(".scenario-btn").forEach((btn) => {
    btn.addEventListener("click", withBusyButton(btn, async () => {
      const hospitalId = $("#simHospital").value;
      const scenario = btn.dataset.scenario;
      const h = state.hospitals[hospitalId];
      let body = {};
      if (scenario === "icu-zero") body = { icu_beds_free: 0 };
      else if (scenario === "ed-overload") body = { ed_bays_occupied: h ? h.ed_bays_total : undefined };

      const result = await postJSON(`/api/hospitals/${hospitalId}/capacity`, body);
      if (result) {
        toast("Scenario disruption applied.", "success");
      }
    }));
  });

  const addAmbBtn = $("#addSecondAmbBtn");
  if (addAmbBtn) {
    addAmbBtn.addEventListener("click", withBusyButton(addAmbBtn, async () => {
      const jitter = () => (Math.random() - 0.5) * 0.02;
      const incident = state.scenario.incident_location;
      const trip = await postJSON("/api/trips", {
        autopilot: true,
        incident_location: { lat: incident.lat + jitter(), lng: incident.lng + jitter() },
      });
      if (trip) {
        toast(`${trip.ambulance_label} launched on autopilot demo.`, "success");
      }
    }));
  }

  const rerouteAcc = $("#rerouteAccept");
  if (rerouteAcc) {
    rerouteAcc.addEventListener("click", withBusyButton(rerouteAcc, async () => {
      const trip = await postJSON(`/api/trips/${state.myTripId}/reroute-response`, { accept: true });
      if (trip) {
        state.trip = trip;
        renderAll();
        playAlertSound("success");
      }
    }));
  }

  const resetBtn = $("#resetBtn");
  if (resetBtn) {
    resetBtn.addEventListener("click", withBusyButton(resetBtn, async () => {
      Object.keys(state.ambulanceMarkers).forEach(removeAmbulanceMarker);
      Object.keys(state.routeLines).forEach(removeRouteLine);
      state.allTrips = {};
      if (condSel) condSel.value = "";
      if (assessBtn) assessBtn.disabled = true;
      await postJSON("/api/reset");
      await startMyTrip();
      const hospitals = await getJSON("/api/hospitals");
      if (hospitals) {
        state.hospitals = Object.fromEntries(hospitals.map((h) => [h.id, h]));
        renderHospitalsOnMap(state.hospitals);
      }
      renderAll();
      toast("Dispatch environment reset.", "success");
    }));
  }
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

    if (msg.type === "trips_list_updated") {
      state.allTrips = Object.fromEntries(msg.trips.map((t) => [t.id, t]));
      msg.trips.forEach((t) => upsertAmbulanceMarker(t.id, t.ambulance_pos, t.ambulance_label, t.id === state.myTripId));
      renderFleetList();
    }

    if (msg.type === "hospitals_updated") {
      state.hospitals = Object.fromEntries(msg.hospitals.map((h) => [h.id, h]));
      renderHospitalsOnMap(state.hospitals);
      renderAll();
    }
  };
  state.ws.onclose = () => {
    setTimeout(connectWs, 2000);
  };
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
}

async function startMyTrip() {
  const trip = await postJSON("/api/trips", {});
  if (!trip) return;
  state.myTripId = trip.id;
  state.trip = trip;
  const labelEl = $("#myAmbulanceLabel");
  if (labelEl) labelEl.textContent = `(${trip.ambulance_label})`;
  upsertAmbulanceMarker(trip.id, trip.ambulance_pos, trip.ambulance_label, true);
}

async function init() {
  setupNavigation();
  setupAnimatedCounters();
  initTheme();
  setupControls();

  const scenario = await getJSON("/api/scenario");
  if (!scenario) return;
  state.scenario = scenario;

  const hospitalsList = await getJSON("/api/hospitals");
  state.hospitals = Object.fromEntries((hospitalsList || []).map((h) => [h.id, h]));

  try {
    initMap(scenario.incident_location);
    renderHospitalsOnMap(state.hospitals);
  } catch (e) {
    console.error("Map error:", e);
  }
  populateSelects(scenario.conditions, state.hospitals);

  await startMyTrip();
  renderAll();
  connectWs();
}

init();
