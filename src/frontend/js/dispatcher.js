// Dispatcher portal: create emergencies (real AI-assisted triage runs
// server-side), get the real hospital recommendation, request/approve a
// hospital, dispatch an available ambulance, and monitor/reroute/cancel
// trips in progress. Every action is a real API call against the backend
// tested in src/backend/tests/ - nothing here is simulated client-side.
const $ = (sel) => document.querySelector(sel);

let currentUser = null;
let selectedEmergencyId = null;

function toast(message, kind = "info") {
  const stack = $("#toastStack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function severityBadge(severity) {
  const cls = { Red: "badge-red", Yellow: "badge-amber", Green: "badge-green" }[severity] || "badge-neutral";
  return `<span class="badge ${cls}">${escapeHtml(severity || "-")}</span>`;
}

function statusBadge(status) {
  const bad = new Set(["CANCELLED", "DECLINED"]);
  const good = new Set(["COMPLETED", "ACCEPTED"]);
  const cls = bad.has(status) ? "badge-red" : good.has(status) ? "badge-green" : "badge-neutral";
  return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
}

async function withBusy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  try { await fn(); } finally { btn.disabled = false; }
}

// --------------------------------------------------------------- Create ---
$("#useLocationBtn").addEventListener("click", () => {
  if (!navigator.geolocation) { toast("Geolocation not available in this browser", "error"); return; }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $("#lat").value = pos.coords.latitude.toFixed(6);
      $("#lng").value = pos.coords.longitude.toFixed(6);
    },
    (err) => toast(`Could not get location: ${err.message}`, "error"),
    { enableHighAccuracy: true, timeout: 10000 },
  );
});

$("#emergencyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#createEmergencyBtn");
  await withBusy(btn, async () => {
    const patient = {};
    if ($("#patientName").value.trim()) patient.name = $("#patientName").value.trim();
    if ($("#patientAge").value) patient.age = Number($("#patientAge").value);
    if ($("#patientGender").value) patient.gender = $("#patientGender").value;

    const vitals = {};
    if ($("#heartRate").value) vitals.heart_rate = Number($("#heartRate").value);
    if ($("#spo2").value) vitals.spo2 = Number($("#spo2").value);
    if ($("#systolicBp").value) vitals.systolic_bp = Number($("#systolicBp").value);
    if ($("#diastolicBp").value) vitals.diastolic_bp = Number($("#diastolicBp").value);
    if ($("#respRate").value) vitals.respiratory_rate = Number($("#respRate").value);
    if ($("#conscious").value) vitals.conscious = $("#conscious").value === "true";

    const payload = {
      symptoms: $("#symptoms").value.trim(),
      lat: Number($("#lat").value),
      lng: Number($("#lng").value),
    };
    if (Object.keys(patient).length) payload.patient = patient;
    if ($("#emergencyType").value.trim()) payload.emergency_type = $("#emergencyType").value.trim();
    if (Object.keys(vitals).length) payload.vitals = vitals;

    try {
      const emergency = await Api.createEmergency(payload);
      toast(`Emergency created - ${emergency.triage.condition_label} (${emergency.triage.source === "watsonx" ? "AI" : "keyword"} triage)`, "success");
      $("#emergencyForm").reset();
      await loadEmergencies();
      selectEmergency(emergency.id);
    } catch (err) {
      toast(err.message || "Failed to create emergency", "error");
    }
  });
});

// ------------------------------------------------------- Emergency list ---
async function loadEmergencies() {
  try {
    const emergencies = await Api.getEmergencies();
    renderEmergencyList(emergencies);
  } catch (err) {
    $("#emergencyList").innerHTML = `<p class="muted">Could not load emergencies: ${escapeHtml(err.message)}</p>`;
  }
}

function renderEmergencyList(emergencies) {
  const container = $("#emergencyList");
  if (!emergencies.length) {
    container.innerHTML = `<p class="muted">No emergencies yet.</p>`;
    return;
  }
  container.innerHTML = emergencies.map((e) => `
    <div class="list-item" data-id="${e.id}" role="button" tabindex="0">
      <div class="row">
        <div>
          <b>${escapeHtml(e.patient && e.patient.name ? e.patient.name : "Unnamed patient")}</b>
          ${severityBadge(e.severity)}
          <span class="muted">${escapeHtml(e.condition_code || "")}</span>
        </div>
        ${statusBadge(e.status)}
      </div>
      <div class="muted">${escapeHtml(new Date(e.created_at).toLocaleString())}</div>
    </div>
  `).join("");
  container.querySelectorAll(".list-item").forEach((el) => {
    el.addEventListener("click", () => selectEmergency(el.dataset.id));
    el.addEventListener("keypress", (ev) => { if (ev.key === "Enter") selectEmergency(el.dataset.id); });
  });
}

// ----------------------------------------------------------------- Detail
async function selectEmergency(id) {
  selectedEmergencyId = id;
  $("#detailCard").hidden = false;
  $("#detailBody").innerHTML = `<p class="muted">Loading...</p>`;
  await renderDetail();
  $("#detailCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function renderDetail() {
  if (!selectedEmergencyId) return;
  let emergency;
  try {
    emergency = await Api.getEmergency(selectedEmergencyId);
  } catch (err) {
    $("#detailBody").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    return;
  }

  let html = `
    <div class="row" style="justify-content:space-between; align-items:flex-start;">
      <div>
        <h3 style="margin:0;">${escapeHtml(emergency.patient && emergency.patient.name ? emergency.patient.name : "Unnamed patient")}</h3>
        <div class="muted">${escapeHtml(emergency.condition_code || "")} ${severityBadge(emergency.severity)} ${statusBadge(emergency.status)}</div>
      </div>
    </div>
  `;

  if (emergency.triage) {
    html += `
      <div class="card" style="margin-top:12px;">
        <div class="decision-note muted" style="margin-bottom:8px;"><b>AI-assisted decision support</b> - not a diagnosis. Final decisions remain under authorized clinical/dispatch control.</div>
        <p><b>${escapeHtml(emergency.triage.condition_label)}</b> - ${escapeHtml(emergency.triage.reasoning)}</p>
        <p class="muted">Requires ICU: ${emergency.triage.requires_icu ? "yes" : "no"} - Requires ED: ${emergency.triage.requires_ed ? "yes" : "no"}
        ${emergency.triage.required_specializations.length ? " - Specialist: " + escapeHtml(emergency.triage.required_specializations.join(", ")) : ""}</p>
      </div>
    `;
  }

  if (["NEW", "ASSESSING", "AWAITING_HOSPITAL"].includes(emergency.status)) {
    html += `<button class="btn btn-primary" id="getRecommendationBtn" style="margin-top:12px;">Get hospital recommendation</button>`;
    html += `<div id="recommendationArea"></div>`;
    html += `<button class="btn btn-ghost" id="cancelEmergencyBtn" style="margin-top:12px;">Cancel emergency</button>`;
  }

  if (emergency.status === "HOSPITAL_ACCEPTED") {
    html += `<div id="dispatchArea" style="margin-top:12px;"><p class="muted">Loading ambulances...</p></div>`;
  }

  $("#detailBody").innerHTML = html;

  const recBtn = $("#getRecommendationBtn");
  if (recBtn) recBtn.addEventListener("click", () => withBusy(recBtn, () => loadRecommendation(emergency.id)));

  const cancelBtn = $("#cancelEmergencyBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", () => withBusy(cancelBtn, async () => {
    try {
      await Api.cancelEmergency(emergency.id);
      toast("Emergency cancelled", "success");
      await renderDetail();
      await loadEmergencies();
    } catch (err) { toast(err.message, "error"); }
  }));

  const dispatchArea = $("#dispatchArea");
  if (dispatchArea) await renderDispatchArea(emergency.id);
}

async function loadRecommendation(emergencyId) {
  const area = $("#recommendationArea");
  area.innerHTML = `<p class="muted">Computing recommendation from live hospital data...</p>`;
  try {
    const rec = await Api.getRecommendation(emergencyId);
    area.innerHTML = `
      <p class="muted" style="margin-top:12px;">Ranked by total estimated time-to-treatment (travel + hospital wait), not distance.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Hospital</th><th>Status</th><th>Delay</th><th></th></tr></thead>
          <tbody>
            ${rec.candidates.map((c) => `
              <tr>
                <td>${escapeHtml(c.name)}${c.hospital_id === rec.recommended_hospital_id ? ' <span class="badge badge-green">Recommended</span>' : ""}</td>
                <td>${c.viable ? '<span class="badge badge-green">Viable</span>' : '<span class="badge badge-red">Not viable</span>'}</td>
                <td>${c.viable ? Math.round(c.est_total_treatment_delay_min) + " min" : "-"}</td>
                <td>${c.viable ? `<button class="btn btn-primary request-hospital-btn" data-hospital-id="${c.hospital_id}">Request</button>` : ""}</td>
              </tr>
              <tr><td colspan="4" class="muted" style="font-size:0.8rem;">${escapeHtml(c.reasons.join("; "))}</td></tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
    area.querySelectorAll(".request-hospital-btn").forEach((btn) => {
      btn.addEventListener("click", () => withBusy(btn, async () => {
        try {
          await Api.requestHospital(emergencyId, btn.dataset.hospitalId);
          toast("Hospital requested - awaiting response", "success");
          await renderDetail();
          await loadEmergencies();
        } catch (err) { toast(err.message, "error"); }
      }));
    });
  } catch (err) {
    area.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

async function renderDispatchArea(emergencyId) {
  const area = $("#dispatchArea");
  try {
    const ambulances = await Api.getAmbulances();
    const available = ambulances.filter((a) => a.status === "AVAILABLE");
    if (!available.length) {
      area.innerHTML = `<p class="muted">No available ambulances right now.</p>`;
      return;
    }
    area.innerHTML = `
      <label for="ambulanceSelect">Dispatch ambulance</label>
      <select id="ambulanceSelect">
        ${available.map((a) => `<option value="${a.id}">${escapeHtml(a.vehicle_number)}</option>`).join("")}
      </select>
      <button class="btn btn-primary full" id="dispatchBtn" style="margin-top:8px;">Dispatch</button>
    `;
    $("#dispatchBtn").addEventListener("click", () => withBusy($("#dispatchBtn"), async () => {
      try {
        await Api.createTrip(emergencyId, $("#ambulanceSelect").value);
        toast("Ambulance dispatched", "success");
        await renderDetail();
        await loadEmergencies();
      } catch (err) { toast(err.message, "error"); }
    }));
  } catch (err) {
    area.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

// ------------------------------------------------------------ Notifications
async function loadNotifications() {
  try {
    const notifications = await Api.getNotifications();
    const container = $("#notificationList");
    if (!notifications.length) {
      container.innerHTML = `<p class="muted">No notifications.</p>`;
      return;
    }
    container.innerHTML = notifications.slice(0, 20).map((n) => `
      <div class="list-item">
        <div class="row"><b>${escapeHtml(n.title)}</b><span class="muted">${escapeHtml(new Date(n.created_at).toLocaleTimeString())}</span></div>
        <div class="muted">${escapeHtml(n.message)}</div>
      </div>
    `).join("");
  } catch (err) {
    $("#notificationList").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

// ------------------------------------------------------------------- Init
async function init() {
  currentUser = await Auth.requireRole("DISPATCHER", "ADMIN");
  if (!currentUser) return;
  $("#userName").textContent = currentUser.name;
  $("#logoutBtn").addEventListener("click", Auth.handleLogout);

  await loadEmergencies();
  await loadNotifications();

  Realtime.onStatusChange = (status) => {
    $("#connDot").className = `conn-dot ${status === "open" ? "open" : status === "closed" ? "closed" : ""}`;
    $("#connLabel").textContent = status === "open" ? "live" : status === "connecting" ? "connecting..." : "offline";
  };
  Realtime.on("*", (msg) => {
    const interesting = new Set([
      "EMERGENCY_CREATED", "HOSPITAL_REQUEST_CREATED", "HOSPITAL_ACCEPTED", "HOSPITAL_DECLINED",
      "TRIP_CREATED", "TRIP_STARTED", "TRIP_ARRIVED", "TRIP_COMPLETED", "TRIP_REROUTED", "TRIP_CANCELLED",
      "NOTIFICATION_CREATED", "HOSPITAL_CAPACITY_UPDATED",
    ]);
    if (interesting.has(msg.type)) {
      loadEmergencies();
      loadNotifications();
      if (selectedEmergencyId && msg.emergency_id === selectedEmergencyId) renderDetail();
    }
  });
  Realtime.connect();
}

init();
