// Hospital portal: view/update own resources+specialists, accept/decline
// incoming requests, and confirm transfer completion for arrived trips.
// Everything here is scoped server-side to the caller's own hospital_id -
// this file never trusts a hospital_id it didn't get back from the API for
// the logged-in user.
const $ = (sel) => document.querySelector(sel);

let currentUser = null;
let hospitalId = null;

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

async function withBusy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  try { await fn(); } finally { btn.disabled = false; }
}

const RESOURCE_FIELDS = [
  ["icu_beds_total", "ICU beds (total)"], ["icu_beds_free", "ICU beds (free)"],
  ["ward_beds_total", "Ward beds (total)"], ["ward_beds_free", "Ward beds (free)"],
  ["ed_bays_total", "ED bays (total)"], ["ed_bays_occupied", "ED bays (occupied)"],
  ["ventilator_total", "Ventilators (total)"], ["ventilator_available", "Ventilators (available)"],
];

async function loadHospital() {
  const hospital = await Api.getHospital(hospitalId);
  $("#hospitalName").textContent = hospital.name;
  $("#hospitalMeta").innerHTML = `
    ${hospital.verification_status === "VERIFIED" ? '<span class="badge badge-green">Verified</span>' : `<span class="badge badge-amber">${escapeHtml(hospital.verification_status)}</span>`}
    <label style="display:inline-block; margin-left:12px;">
      Status:
      <select id="availabilitySelect" style="display:inline-block; width:auto;">
        ${["ONLINE", "BUSY", "FULL", "OFFLINE"].map((s) => `<option value="${s}" ${s === hospital.availability_status ? "selected" : ""}>${s}</option>`).join("")}
      </select>
    </label>
  `;
  $("#availabilitySelect").addEventListener("change", async (e) => {
    try {
      await Api.updateHospitalResources(hospitalId, { availability_status: e.target.value });
      toast("Status updated", "success");
    } catch (err) { toast(err.message, "error"); }
  });

  const form = $("#resourcesForm");
  form.innerHTML = RESOURCE_FIELDS.map(([field, label]) => `
    <div>
      <label for="rf_${field}">${label}</label>
      <input type="number" id="rf_${field}" min="0" value="${hospital.resources ? hospital.resources[field] : 0}" />
    </div>
  `).join("");

  renderSpecialists(hospital.specialists);
}

function renderSpecialists(specialists) {
  const container = $("#specialistList");
  if (!specialists.length) {
    container.innerHTML = `<p class="muted">No specialists recorded yet.</p>`;
    return;
  }
  container.innerHTML = specialists.map((s) => `
    <div class="list-item">
      <div class="row">
        <div><b>${escapeHtml(s.type)}</b>${s.doctor_name ? " - " + escapeHtml(s.doctor_name) : ""}</div>
        <label style="display:flex; align-items:center; gap:6px; margin:0;">
          <input type="checkbox" data-specialist-id="${s.id}" class="on-duty-toggle" ${s.on_duty ? "checked" : ""} style="width:auto;" />
          On duty
        </label>
      </div>
    </div>
  `).join("");
  container.querySelectorAll(".on-duty-toggle").forEach((cb) => {
    cb.addEventListener("change", async () => {
      try {
        await Api.updateSpecialist(hospitalId, cb.dataset.specialistId, { on_duty: cb.checked });
        toast("Specialist availability updated", "success");
      } catch (err) {
        toast(err.message, "error");
        cb.checked = !cb.checked;
      }
    });
  });
}

$("#resourcesForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#saveResourcesBtn");
  await withBusy(btn, async () => {
    const payload = {};
    RESOURCE_FIELDS.forEach(([field]) => {
      const input = $(`#rf_${field}`);
      if (input && input.value !== "") payload[field] = Number(input.value);
    });
    try {
      await Api.updateHospitalResources(hospitalId, payload);
      toast("Resources saved", "success");
      await loadHospital();
    } catch (err) {
      toast(err.message || "Failed to save resources", "error");
    }
  });
});

$("#addSpecialistBtn").addEventListener("click", () => withBusy($("#addSpecialistBtn"), async () => {
  const type = $("#newSpecialistType").value.trim();
  if (!type) return;
  try {
    await Api.createSpecialist(hospitalId, { type });
    $("#newSpecialistType").value = "";
    toast("Specialist added", "success");
    await loadHospital();
  } catch (err) { toast(err.message, "error"); }
}));

// ------------------------------------------------------------- Requests --
async function loadRequests() {
  const container = $("#requestList");
  try {
    const requests = await Api.getHospitalRequests(hospitalId);
    const pending = requests.filter((r) => r.status === "PENDING");
    if (!pending.length) {
      container.innerHTML = `<p class="muted">No pending requests.</p>`;
      return;
    }
    container.innerHTML = pending.map((r) => `
      <div class="list-item" data-request-id="${r.id}">
        <div class="row">
          <div>Emergency <span class="muted">${escapeHtml(r.emergency_id.slice(0, 8))}</span></div>
          <span class="badge badge-amber">PENDING</span>
        </div>
        <div class="row" style="margin-top:8px;">
          <button class="btn btn-success accept-btn" data-id="${r.id}">Accept</button>
          <button class="btn btn-danger decline-btn" data-id="${r.id}">Decline</button>
        </div>
      </div>
    `).join("");
    container.querySelectorAll(".accept-btn").forEach((btn) => {
      btn.addEventListener("click", () => withBusy(btn, async () => {
        try {
          await Api.acceptHospitalRequest(btn.dataset.id);
          toast("Request accepted - capacity reserved", "success");
          await loadRequests();
        } catch (err) { toast(err.message, "error"); }
      }));
    });
    container.querySelectorAll(".decline-btn").forEach((btn) => {
      btn.addEventListener("click", () => withBusy(btn, async () => {
        const reason = prompt("Reason for declining (optional):") || "";
        try {
          await Api.declineHospitalRequest(btn.dataset.id, reason);
          toast("Request declined", "success");
          await loadRequests();
        } catch (err) { toast(err.message, "error"); }
      }));
    });
  } catch (err) {
    container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

// ----------------------------------------------------------------- Trips -
async function loadTrips() {
  const container = $("#tripList");
  try {
    const trips = await Api.getHospitalTrips(hospitalId);
    const active = trips.filter((t) => !["CANCELLED", "COMPLETED"].includes(t.status));
    if (!active.length) {
      container.innerHTML = `<p class="muted">No inbound trips.</p>`;
      return;
    }
    container.innerHTML = active.map((t) => `
      <div class="list-item">
        <div class="row">
          <div>Trip <span class="muted">${escapeHtml(t.id.slice(0, 8))}</span></div>
          <span class="badge badge-neutral">${escapeHtml(t.status)}</span>
        </div>
        ${t.status === "ARRIVED" ? `<button class="btn btn-success complete-btn" data-id="${t.id}" style="margin-top:8px;">Confirm transfer complete</button>` : ""}
      </div>
    `).join("");
    container.querySelectorAll(".complete-btn").forEach((btn) => {
      btn.addEventListener("click", () => withBusy(btn, async () => {
        try {
          await Api.completeTrip(btn.dataset.id);
          toast("Transfer completed", "success");
          await loadTrips();
        } catch (err) { toast(err.message, "error"); }
      }));
    });
  } catch (err) {
    container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

// ------------------------------------------------------------ Notifications
async function loadNotifications() {
  const container = $("#notificationList");
  try {
    const notifications = await Api.getNotifications();
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
    container.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

// ------------------------------------------------------------------- Init
async function init() {
  currentUser = await Auth.requireRole("HOSPITAL_ADMIN");
  if (!currentUser) return;
  hospitalId = currentUser.hospital_id;
  if (!hospitalId) {
    toast("Your account has no hospital assigned - contact an administrator.", "error");
    return;
  }
  $("#userName").textContent = currentUser.name;
  $("#logoutBtn").addEventListener("click", Auth.handleLogout);

  await loadHospital();
  await loadRequests();
  await loadTrips();
  await loadNotifications();

  Realtime.onStatusChange = (status) => {
    $("#connDot").className = `conn-dot ${status === "open" ? "open" : status === "closed" ? "closed" : ""}`;
    $("#connLabel").textContent = status === "open" ? "live" : status === "connecting" ? "connecting..." : "offline";
  };
  Realtime.on("*", (msg) => {
    const interesting = new Set([
      "HOSPITAL_REQUEST_CREATED", "TRIP_CREATED", "TRIP_ARRIVED", "TRIP_REROUTED",
      "HOSPITAL_CAPACITY_UPDATED", "SPECIALIST_AVAILABILITY_UPDATED", "NOTIFICATION_CREATED",
    ]);
    if (msg.hospital_id && msg.hospital_id !== hospitalId) return;
    if (interesting.has(msg.type)) {
      loadRequests();
      loadTrips();
      loadNotifications();
      if (msg.type === "HOSPITAL_CAPACITY_UPDATED" || msg.type === "SPECIALIST_AVAILABILITY_UPDATED") loadHospital();
    }
  });
  Realtime.connect();
}

init();
