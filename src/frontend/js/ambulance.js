// Ambulance portal: real GPS via navigator.geolocation.watchPosition,
// streamed to the backend at a throttled interval, plus trip lifecycle
// controls (start/arrive). No simulated movement anywhere in this file -
// every position shown/sent comes from the device's actual GPS.
const $ = (sel) => document.querySelector(sel);

let currentUser = null;
let myAmbulance = null;
let watchId = null;
let lastSentAt = 0;
const SEND_INTERVAL_MS = 5000; // matches ambulance_routes.py's TripLocation throttle

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

function renderStatus() {
  $("#vehicleNumber").textContent = myAmbulance.vehicle_number;
  const cls = { AVAILABLE: "badge-green", OFFLINE: "badge-neutral", EN_ROUTE: "badge-amber" }[myAmbulance.status] || "badge-neutral";
  $("#statusBadge").innerHTML = `<span class="badge ${cls}">${escapeHtml(myAmbulance.status)}</span>`;
  $("#lastUpdateLine").textContent = myAmbulance.last_location_update
    ? `Last GPS update: ${new Date(myAmbulance.last_location_update).toLocaleTimeString()}`
    : "No GPS update sent yet";
}

// ------------------------------------------------------------------- GPS -
function startGps() {
  if (!navigator.geolocation) { toast("Geolocation not supported on this device/browser", "error"); return; }

  $("#gpsStatus").textContent = "Acquiring...";
  watchId = navigator.geolocation.watchPosition(
    async (position) => {
      $("#gpsStatus").textContent = "Active";
      const now = Date.now();
      if (now - lastSentAt < SEND_INTERVAL_MS) return; // client-side throttle mirrors the server's own
      lastSentAt = now;

      try {
        const updated = await Api.sendLocation(myAmbulance.id, {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          speed: position.coords.speed !== null ? position.coords.speed * 3.6 : null, // m/s -> km/h
          heading: position.coords.heading !== null ? position.coords.heading : null,
        });
        myAmbulance = updated;
        renderStatus();
      } catch (err) {
        toast(`GPS update failed: ${err.message}`, "error");
      }
    },
    (err) => {
      $("#gpsStatus").textContent = "GPS unavailable";
      toast(`GPS error: ${err.message}`, "error");
    },
    { enableHighAccuracy: true, maximumAge: 2000, timeout: 15000 },
  );
  $("#gpsToggleBtn").textContent = "Stop GPS";
}

function stopGps() {
  if (watchId !== null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
  $("#gpsStatus").textContent = "Stopped";
  $("#gpsToggleBtn").textContent = "Start GPS";
}

$("#gpsToggleBtn").addEventListener("click", () => {
  if (watchId === null) startGps(); else stopGps();
});

// ------------------------------------------------------------------ Trip -
async function loadTrip() {
  try {
    const trip = await Api.getMyTrip();
    $("#tripCard").hidden = false;
    $("#noTripCard").hidden = true;
    await renderTrip(trip);
  } catch (err) {
    if (err.status === 404) {
      $("#tripCard").hidden = true;
      $("#noTripCard").hidden = false;
    } else {
      toast(err.message, "error");
    }
  }
}

async function renderTrip(trip) {
  let hospitalName = trip.dest_hospital_id;
  try {
    const hospital = await Api.getHospital(trip.dest_hospital_id);
    hospitalName = hospital.name;
  } catch { /* show the id if the hospital lookup fails - not fatal */ }

  const body = $("#tripBody");
  body.innerHTML = `
    <p><b>Destination:</b> ${escapeHtml(hospitalName)}</p>
    <p><b>Status:</b> <span class="badge badge-neutral">${escapeHtml(trip.status)}</span></p>
    <div id="tripActions" style="margin-top:10px;"></div>
  `;
  const actions = $("#tripActions");
  if (trip.status === "DISPATCHED") {
    actions.innerHTML = `<button class="btn btn-primary full" id="startBtn">Start trip</button>`;
    $("#startBtn").addEventListener("click", () => withBusy($("#startBtn"), async () => {
      try {
        await Api.startTrip(trip.id);
        toast("Trip started", "success");
        await loadTrip();
      } catch (err) { toast(err.message, "error"); }
    }));
  } else if (trip.status === "EN_ROUTE" || trip.status === "ARRIVING") {
    actions.innerHTML = `<button class="btn btn-success full" id="arriveBtn">Confirm arrival</button>`;
    $("#arriveBtn").addEventListener("click", () => withBusy($("#arriveBtn"), async () => {
      try {
        await Api.arriveTrip(trip.id);
        toast("Arrival confirmed", "success");
        await loadTrip();
      } catch (err) { toast(err.message, "error"); }
    }));
  } else if (trip.status === "ARRIVED") {
    actions.innerHTML = `<p class="muted">Waiting for the receiving hospital to confirm transfer.</p>`;
  }
}

// ------------------------------------------------------------------- Init
async function init() {
  currentUser = await Auth.requireRole("AMBULANCE_OPERATOR");
  if (!currentUser) return;
  $("#logoutBtn").addEventListener("click", () => { stopGps(); Auth.handleLogout(); });

  try {
    myAmbulance = await Api.getMyAmbulance();
    renderStatus();
  } catch (err) {
    $("#vehicleNumber").textContent = "No ambulance assigned";
    toast(err.message || "No ambulance is assigned to your account - contact your dispatcher.", "error");
    return;
  }

  await loadTrip();

  Realtime.onStatusChange = (status) => {
    $("#connDot").className = `conn-dot ${status === "open" ? "open" : status === "closed" ? "closed" : ""}`;
    $("#connLabel").textContent = status === "open" ? "connected" : status === "connecting" ? "connecting..." : "disconnected";
  };
  Realtime.on("*", (msg) => {
    const interesting = new Set(["TRIP_CREATED", "TRIP_REROUTED", "TRIP_STARTED", "TRIP_ARRIVED", "TRIP_COMPLETED", "TRIP_CANCELLED"]);
    if (interesting.has(msg.type)) loadTrip();
  });
  Realtime.connect();

  window.addEventListener("beforeunload", stopGps);
}

init();
