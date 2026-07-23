// Same-origin API client. The bearer token lives only in this module's memory —
// never in localStorage, sessionStorage, cookies, URLs, or logs.
let bearerToken = "";

export function setToken(value) {
  bearerToken = String(value || "").trim();
}

export function clearToken() {
  bearerToken = "";
}

export function hasToken() {
  return Boolean(bearerToken);
}

export async function request(path, options = {}) {
  if (!bearerToken) throw new Error("authentication required");
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${bearerToken}`);
  headers.set("Accept", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) clearToken();
  return response;
}

export async function requestJson(path, options = {}) {
  const response = await request(path, options);
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  return { status: response.status, ok: response.ok, body };
}
