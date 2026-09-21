async function req(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

export const api = {
  session: () => req("/api/session"),
  health: () => req("/api/health"),
  login: (token) => req("/api/auth/login", { method: "POST", body: JSON.stringify({ token }) }),
  logout: () => req("/api/auth/logout", { method: "POST" }),
  requestDevice: () => req("/api/auth/device/request", { method: "POST", body: "{}" }),
  claimDevice: (code) =>
    req("/api/auth/device/claim", { method: "POST", body: JSON.stringify({ code }) }),
  status: () => req("/api/status"),
  commands: () => req("/api/commands"),
  models: () => req("/api/models"),
  setProvider: (name) => req("/api/provider", { method: "POST", body: JSON.stringify({ name }) }),
  setModel: (id) => req("/api/model", { method: "POST", body: JSON.stringify({ id }) }),
  devices: () => req("/api/devices"),
  revokeDevice: (code) =>
    req("/api/devices/revoke", { method: "POST", body: JSON.stringify({ code }) }),
  rotateToken: () => req("/api/token/rotate", { method: "POST", body: "{}" }),
  chat: (text) => req("/api/chat", { method: "POST", body: JSON.stringify({ text }) }),
};

export function chatSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${proto}://${location.host}/api/ws`);
}
