// SrijanX - marketing/landing page only.
//
// The real portals (login.html, dispatcher.html, hospital.html,
// ambulance.html) are separate pages with their own scripts (js/*.js) - this
// page has no #map, #conditionSelect, #hospTable, or any other portal
// element for that code to attach to. This file contains only what the
// landing page's own DOM (nav, animated stat counters, theme/sound toggles)
// needs.

const state = {
  theme: localStorage.getItem("theme") || "light",
  soundMuted: localStorage.getItem("soundMuted") === "true",
  // map/tileLayer stay null forever on this page (no #map element here) -
  // setTheme()'s map-retheming branch below is a no-op as a result, which
  // is correct: it only does anything on a page that actually has a map.
  map: null,
  tileLayer: null,
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

// ------------------------------------------------------------- Init ----
async function init() {
  setupNavigation();
  setupAnimatedCounters();
  initTheme();
}

init();
