// Reads the muse.ai session cookies in this browser and posts them to a museai instance.
// The server parses the Cookie header itself, so no field mapping is duplicated here.

const REQUIRED = ["hatch_sess", "hatch_gw", "hatch_native_auth_device"];
const COOKIE_DOMAINS = ["muse.ai", ".muse.ai"];

const $ = (id) => document.getElementById(id);

function setStatus(text, cls) {
  const node = $("status");
  node.textContent = text;
  node.className = cls || "";
}

async function loadSettings() {
  const stored = await chrome.storage.local.get(["url", "key", "label"]);
  for (const [field, value] of Object.entries(stored)) {
    if (value && $(field)) $(field).value = value;
  }
}

async function collectCookies() {
  const seen = new Map();
  for (const domain of COOKIE_DOMAINS) {
    const cookies = await chrome.cookies.getAll({ domain });
    for (const cookie of cookies) {
      seen.set(cookie.name, cookie.value);
    }
  }
  const header = [...seen].map(([name, value]) => `${name}=${value}`).join("; ");
  const missing = REQUIRED.filter((name) => !seen.has(name));
  return { header, missing };
}

$("send").addEventListener("click", async () => {
  const url = $("url").value.replace(/\/+$/, "");
  const key = $("key").value.trim();
  const label = $("label").value.trim();
  if (!url || !key) {
    setStatus("URL and admin key are required.", "err");
    return;
  }
  await chrome.storage.local.set({ url, key, label });
  setStatus("Reading cookies…");

  const { header, missing } = await collectCookies();
  if (!header) {
    setStatus("No muse.ai cookies found. Log in at https://muse.ai first.", "err");
    return;
  }

  try {
    const response = await fetch(`${url}/admin/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({ cookies_raw: header, label: label || "browser" }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = data?.error?.message || `HTTP ${response.status}`;
      setStatus(`Import failed: ${message}`, "err");
      return;
    }
    const warnings = data.missing_cookies?.length
      ? `\nMissing cookies: ${data.missing_cookies.join(", ")}`
      : "";
    setStatus(`Imported as ${data.account.id}${warnings}`, warnings || missing.length ? "err" : "ok");
  } catch (error) {
    setStatus(`Import failed: ${error.message}`, "err");
  }
});

loadSettings();
