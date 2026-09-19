// Real-time event stream. Delivery only - every page still loads its
// actual state from the REST API (api.js) on page load and after any
// action; a dropped/reconnecting socket never leaves a page showing stale
// data as if it were current (Rule: the DB/API remains authoritative, the
// websocket is not a second source of truth).
class RealtimeClient {
  constructor() {
    this.ws = null;
    this.listeners = {};
    this.reconnectDelayMs = 1000;
    this.maxReconnectDelayMs = 15000;
    this.manuallyClosed = false;
    this.onStatusChange = null; // optional: fn(status) - "connecting"|"open"|"closed"
  }

  connect() {
    const token = (typeof Api !== "undefined" && Api.getStoredUser) ? localStorage.getItem("srijanx_access_token") : null;
    if (!token) return;

    this.manuallyClosed = false;
    this._setStatus("connecting");
    this.ws = new WebSocket(Api.wsUrl(token));

    this.ws.onopen = () => {
      this.reconnectDelayMs = 1000;
      this._setStatus("open");
    };
    this.ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      const handlers = this.listeners[msg.type] || [];
      for (const fn of handlers) {
        try { fn(msg); } catch (e) { console.error("websocket handler error", e); }
      }
      const wildcardHandlers = this.listeners["*"] || [];
      for (const fn of wildcardHandlers) {
        try { fn(msg); } catch (e) { console.error("websocket handler error", e); }
      }
    };
    this.ws.onclose = (event) => {
      this._setStatus("closed");
      // 4401: the token was rejected (expired/rotated) - don't hammer the
      // server retrying with a token that will never be accepted again.
      if (event.code === 4401 || this.manuallyClosed) return;
      setTimeout(() => this.connect(), this.reconnectDelayMs);
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, this.maxReconnectDelayMs);
    };
    this.ws.onerror = () => {
      try { this.ws.close(); } catch { /* already closing */ }
    };
  }

  close() {
    this.manuallyClosed = true;
    if (this.ws) this.ws.close();
  }

  on(eventType, handler) {
    if (!this.listeners[eventType]) this.listeners[eventType] = [];
    this.listeners[eventType].push(handler);
  }

  _setStatus(status) {
    if (this.onStatusChange) this.onStatusChange(status);
  }
}

window.Realtime = new RealtimeClient();
