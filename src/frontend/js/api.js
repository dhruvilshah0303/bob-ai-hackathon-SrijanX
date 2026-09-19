// Centralized API client for the real SrijanX backend - every page (login,
// dispatcher, hospital, ambulance) calls through this instead of scattering
// raw fetch() calls, so the auth-header/refresh/error-shape logic exists in
// exactly one place.
//
// BACKEND_ORIGIN: same convention as the landing page's app.js
// (window.COORDINATOR_BACKEND_URL, set in config.js) - empty string means
// "same origin as this page" (the bundled single-container deployment).
const BACKEND_ORIGIN = (window.COORDINATOR_BACKEND_URL || "").replace(/\/$/, "");
const WS_ORIGIN = (window.COORDINATOR_WS_URL || "").replace(/\/$/, "");

const TOKEN_KEY = "srijanx_access_token";
const REFRESH_KEY = "srijanx_refresh_token";
const USER_KEY = "srijanx_user";

function getAccessToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
function getRefreshToken() {
  try { return localStorage.getItem(REFRESH_KEY) || ""; } catch { return ""; }
}
function getStoredUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch { return null; }
}
function storeSession(tokenResponse) {
  try {
    localStorage.setItem(TOKEN_KEY, tokenResponse.access_token);
    localStorage.setItem(REFRESH_KEY, tokenResponse.refresh_token);
    localStorage.setItem(USER_KEY, JSON.stringify(tokenResponse.user));
  } catch { /* localStorage unavailable (private browsing etc) - session just won't persist across reloads */ }
}
function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
  } catch { /* nothing to clear */ }
}

let refreshInFlight = null;

async function refreshAccessToken() {
  if (refreshInFlight) return refreshInFlight;
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  refreshInFlight = (async () => {
    try {
      const resp = await fetch(`${BACKEND_ORIGIN}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) return false;
      const data = await resp.json();
      storeSession(data);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

// Every call goes through here. On a 401 (expired access token), tries
// exactly one silent refresh-and-retry before giving up and forcing the
// caller to redirect to /login.html - never loops forever.
async function request(path, options = {}, _isRetry = false) {
  const url = path.startsWith("http") ? path : `${BACKEND_ORIGIN}${path}`;
  const token = getAccessToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let resp;
  try {
    resp = await fetch(url, { ...options, headers });
  } catch (e) {
    throw new ApiError(`Network error: ${e.message}`, 0, null);
  }

  if (resp.status === 401 && !_isRetry && getRefreshToken()) {
    const refreshed = await refreshAccessToken();
    if (refreshed) return request(path, options, true);
  }

  if (!resp.ok) {
    let detail = null;
    try { detail = (await resp.json()).detail; } catch { /* body wasn't JSON */ }
    throw new ApiError(detail || resp.statusText || `HTTP ${resp.status}`, resp.status, detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

const Api = {
  ApiError,
  getStoredUser,
  clearSession,
  wsUrl(token) {
    let base = WS_ORIGIN;
    if (!base) {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      base = `${proto}//${window.location.host}`;
    }
    return `${base}/ws?token=${encodeURIComponent(token)}`;
  },

  // Auth
  async login(email, password) {
    const data = await request("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    storeSession(data);
    return data.user;
  },
  async register(payload) {
    const data = await request("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    storeSession(data);
    return data.user;
  },
  async logout() {
    try { await request("/api/auth/logout", { method: "POST" }); } catch { /* best effort */ }
    clearSession();
  },
  async getCurrentUser() {
    return request("/api/auth/me");
  },

  // Hospitals
  getHospital(id) { return request(`/api/hospitals/${id}`); },
  updateHospitalResources(id, payload) {
    return request(`/api/hospitals/${id}/resources`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  updateSpecialist(hospitalId, specialistId, payload) {
    return request(`/api/hospitals/${hospitalId}/specialists/${specialistId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  createSpecialist(hospitalId, payload) {
    return request(`/api/hospitals/${hospitalId}/specialists`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  getHospitalRequests(hospitalId) { return request(`/api/hospitals/${hospitalId}/hospital-requests`); },
  getHospitalTrips(hospitalId) { return request(`/api/hospitals/${hospitalId}/trips`); },

  // Ambulances
  getAmbulances() { return request("/api/ambulances"); },
  getAmbulance(id) { return request(`/api/ambulances/${id}`); },
  getMyAmbulance() { return request("/api/ambulances/me"); },
  getMyTrip() { return request("/api/trips/mine"); },
  updateAmbulanceStatus(id, statusValue) {
    return request(`/api/ambulances/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: statusValue }),
    });
  },
  sendLocation(id, payload) {
    return request(`/api/ambulances/${id}/location`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  // Emergencies
  createEmergency(payload) {
    return request("/api/emergencies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  getEmergencies() { return request("/api/emergencies"); },
  getEmergency(id) { return request(`/api/emergencies/${id}`); },
  cancelEmergency(id) { return request(`/api/emergencies/${id}/cancel`, { method: "POST" }); },
  getRecommendation(emergencyId) { return request(`/api/emergencies/${emergencyId}/recommendation`); },

  // Hospital requests
  requestHospital(emergencyId, hospitalId) {
    return request("/api/hospital-requests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emergency_id: emergencyId, hospital_id: hospitalId }),
    });
  },
  acceptHospitalRequest(id) { return request(`/api/hospital-requests/${id}/accept`, { method: "POST" }); },
  declineHospitalRequest(id, reason) {
    return request(`/api/hospital-requests/${id}/decline`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || null }),
    });
  },

  // Trips
  createTrip(emergencyId, ambulanceId) {
    return request("/api/trips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emergency_id: emergencyId, ambulance_id: ambulanceId }),
    });
  },
  getTrips() { return request("/api/trips"); },
  getTrip(id) { return request(`/api/trips/${id}`); },
  startTrip(id) { return request(`/api/trips/${id}/start`, { method: "POST" }); },
  arriveTrip(id) { return request(`/api/trips/${id}/arrive`, { method: "POST" }); },
  completeTrip(id) { return request(`/api/trips/${id}/complete`, { method: "POST" }); },
  rerouteTrip(id, newHospitalId) {
    return request(`/api/trips/${id}/reroute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_hospital_id: newHospitalId }),
    });
  },
  cancelTrip(id) { return request(`/api/trips/${id}/cancel`, { method: "POST" }); },

  // Notifications
  getNotifications() { return request("/api/notifications"); },
  markNotificationRead(id) { return request(`/api/notifications/${id}/read`, { method: "POST" }); },

  // Analytics
  getAnalytics() { return request("/api/analytics"); },

  // Admin
  adminCreateUser(payload) {
    return request("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  adminListUsers() { return request("/api/admin/users"); },
  adminUpdateUser(id, payload) {
    return request(`/api/admin/users/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
};

window.Api = Api;
