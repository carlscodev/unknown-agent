async function req(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body && !(options.body instanceof FormData)
      ? { "Content-Type": "application/json" }
      : undefined,
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

const post = (path, payload = {}) => req(path, { method: "POST", body: JSON.stringify(payload) });

export const api = {
  session: () => req("/api/session"),
  health: () => req("/api/health"),
  login: (token) => post("/api/auth/login", { token }),
  logout: () => post("/api/auth/logout"),
  requestDevice: () => post("/api/auth/device/request"),
  claimDevice: (code) => post("/api/auth/device/claim", { code }),
  status: () => req("/api/status"),
  commands: () => req("/api/commands"),
  models: () => req("/api/models"),
  setProvider: (name) => post("/api/provider", { name }),
  setModel: (id) => post("/api/model", { id }),
  stop: () => post("/api/stop"),
  upload: (file) => {
    const form = new FormData();
    form.append("file", file);
    return req("/api/upload", { method: "POST", body: form });
  },
  chat: (text, attachments, agent) => post("/api/chat", { text, attachments, agent }),
  dashboard: () => req("/api/dashboard"),
  addProvider: (p) => post("/api/dashboard/provider", p),
  removeProvider: (name) => post("/api/dashboard/provider/remove", { name }),
  setAgent: (a) => post("/api/dashboard/agent", a),
  mcpAdd: (m) => post("/api/dashboard/mcp/add", m),
  mcpToggle: (name, enabled) => post("/api/dashboard/mcp/toggle", { name, enabled }),
  mcpRemove: (name) => post("/api/dashboard/mcp/remove", { name }),
  setFiles: (f) => post("/api/dashboard/files", f),
  devices: () => req("/api/devices"),
  revokeDevice: (code) => post("/api/devices/revoke", { code }),
  rotateToken: () => post("/api/token/rotate"),
};

export function chatSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${proto}://${location.host}/api/ws`);
}
