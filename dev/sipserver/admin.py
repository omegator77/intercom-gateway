"""Admin page for the local Kamailio test server: who is registered, which calls are running, and the users.

    python admin.py            (inside the compose service; needs only the standard library)

Reads everything from Kamailio's JSON-RPC (http://kamailio:8080/jsonrpc). Users are kept in users.cfg, which Kamailio
loads at start; changes made here are applied to the running server at once and written to that file."""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RPC_URL = os.environ.get("RPC_URL", "http://kamailio:8080/jsonrpc")
USERS_FILE = os.environ.get("USERS_FILE", "/data/users.cfg")
PORT = int(os.environ.get("PORT", "8099"))
REALM = os.environ.get("REALM", "sip.local")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PASSWORD_RE = re.compile(r'^[A-Za-z0-9!#%&()*+,./:;<=>?@^_~-]{4,64}$')  # nothing that means something in the config syntax
LINE_RE = re.compile(r'^\$sht\(cred=>([^)]+)\)\s*=\s*"([^"]*)";\s*$')
lock = threading.Lock()


def rpc(method: str, params: list | None = None):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request(RPC_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "error"))
    return data["result"]


def read_users() -> dict[str, str]:
    users: dict[str, str] = {}
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            for line in f:
                m = LINE_RE.match(line.strip())
                if m:
                    users[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return users


def write_users(users: dict[str, str]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:  # in place: Kamailio has the file bind-mounted
        for name in sorted(users):
            f.write(f'$sht(cred=>{name}) = "{users[name]}";\n')


def state() -> dict:
    out: dict = {"server": None, "registrations": [], "calls": [], "users": [], "error": None}
    try:
        out["server"] = {"version": rpc("core.version").strip(), "uptime": rpc("core.uptime")["uptime"], "realm": REALM}
        for dom in rpc("ul.dump")["Domains"]:
            for aor in dom["Domain"].get("AoRs", []):
                for c in aor["Info"].get("Contacts", []):
                    c = c["Contact"]
                    out["registrations"].append({"user": aor["Info"]["AoR"], "address": c["Address"],
                                                 "agent": c.get("User-Agent", ""), "expires": c["Expires"]})
        for d in rpc("dlg.list") or []:
            out["calls"].append({"from": d["from_uri"], "to": d["to_uri"], "state": d["state"], "duration": d["duration"],
                                 "caller": d["caller"]["contact"], "callee": d["callee"]["contact"]})
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
        out["error"] = f"Kamailio antwortet nicht: {e}"
    users = read_users()
    online = {r["user"] for r in out["registrations"]}
    out["users"] = [{"name": n, "password": users[n], "online": n in online} for n in sorted(users)]
    return out


def apply_user(name: str, password: str | None) -> None:
    """Set (or with None remove) a password in the running server; the file is written by the caller."""
    try:
        if password is None:
            rpc("htable.delete", ["cred", name])
        else:
            rpc("htable.sets", ["cred", name, password])
    except (urllib.error.URLError, OSError, RuntimeError):
        pass  # the server may be down; the file is what it will load next time


class Handler(BaseHTTPRequestHandler):
    server_version = "sip-admin"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/api/state":
            self._json(200, state())
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/users":
            return self._json(404, {"error": "not found"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._json(400, {"error": "ungültige Eingabe"})
        name, password = str(data.get("name", "")).strip(), str(data.get("password", ""))
        if not NAME_RE.match(name):
            return self._json(400, {"error": "Name: 1 bis 32 Zeichen aus Buchstaben, Ziffern, Punkt, Strich, Unterstrich"})
        if not PASSWORD_RE.match(password):
            return self._json(400, {"error": "Passwort: 4 bis 64 Zeichen, ohne Leerzeichen, Anführungszeichen, $ und Backslash"})
        with lock:
            users = read_users()
            users[name] = password
            write_users(users)
            apply_user(name, password)
        self._json(200, {"ok": True})

    def do_DELETE(self):
        m = re.match(r"^/api/users/([^/]+)$", self.path)
        if not m or not NAME_RE.match(m.group(1)):
            return self._json(404, {"error": "not found"})
        with lock:
            users = read_users()
            if users.pop(m.group(1), None) is None:
                return self._json(404, {"error": "Benutzer gibt es nicht"})
            write_users(users)
            apply_user(m.group(1), None)
        self._json(200, {"ok": True})


PAGE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SIP-Testserver</title>
<style>
  :root {
    --bg: #f3f5f4; --panel: #ffffff; --ink: #18211f; --muted: #5d6b67; --line: #dde3e0;
    --accent: #0b6e5f; --accent-ink: #ffffff; --ok: #1a7f4b; --ok-bg: #dcf3e6; --off: #6b7774; --off-bg: #e8ecea;
    --warn: #9a5b00; --warn-bg: #fbecd0; --bad: #a3261d; --bad-bg: #f9dedb; --field: #f7f9f8;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #111715; --panel: #1a2220; --ink: #e6ede9; --muted: #93a29c; --line: #2c3834;
      --accent: #46c2a8; --accent-ink: #08201b; --ok: #6fd9a0; --ok-bg: #173a2a; --off: #93a29c; --off-bg: #25302c;
      --warn: #f0b95a; --warn-bg: #3a2c12; --bad: #ff8d84; --bad-bg: #402220; --field: #141b19;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.45 system-ui, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; display: grid; gap: 16px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
  .meta { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 10px; font-weight: 600; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th { text-align: left; font-weight: 500; color: var(--muted); font-size: 13px; padding: 4px 10px 6px 0; border-bottom: 1px solid var(--line); }
  td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
  tr:last-child td { border-bottom: 0; }
  .empty { color: var(--muted); padding: 6px 0; }
  .pill { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12.5px; font-weight: 500; }
  .pill.ok { background: var(--ok-bg); color: var(--ok); }
  .pill.off { background: var(--off-bg); color: var(--off); }
  .pill.warn { background: var(--warn-bg); color: var(--warn); }
  code, .mono { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 13px; }
  .pw { cursor: pointer; border: 0; background: none; color: inherit; padding: 0; font: inherit; text-align: left; }
  .pw:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  form { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; align-items: end; }
  label { display: grid; gap: 3px; font-size: 13px; color: var(--muted); }
  input { font: inherit; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; background: var(--field); color: var(--ink); min-width: 180px; }
  button { font: inherit; padding: 7px 14px; border-radius: 7px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); cursor: pointer; }
  button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
  button.danger { color: var(--bad); }
  button:hover { filter: brightness(0.97); }
  .banner { padding: 10px 14px; border-radius: 8px; background: var(--bad-bg); color: var(--bad); }
  .msg { font-size: 13px; min-height: 18px; margin-top: 6px; }
  .msg.bad { color: var(--bad); }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; }
  dt { color: var(--muted); }
  dd { margin: 0; }
  @media (prefers-reduced-motion: no-preference) { .pill { transition: background 0.2s; } }
</style>
</head>
<body>
<main>
  <header>
    <h1>SIP-Testserver</h1>
    <div class="meta" id="meta">…</div>
  </header>
  <div id="banner"></div>

  <section>
    <h2>Angemeldet</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Nutzer</th><th>Gerät</th><th>Adresse</th><th>läuft ab in</th></tr></thead>
      <tbody id="regs"></tbody>
    </table></div>
  </section>

  <section>
    <h2>Gespräche</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Von</th><th>Nach</th><th>Dauer</th><th>Zustand</th></tr></thead>
      <tbody id="calls"></tbody>
    </table></div>
  </section>

  <section>
    <h2>Benutzer</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Passwort</th><th>Status</th><th></th></tr></thead>
      <tbody id="users"></tbody>
    </table></div>
    <form id="add" autocomplete="off">
      <label>Name<input name="name" required maxlength="32" placeholder="z. B. luci1"></label>
      <label>Passwort<input name="password" required minlength="4" maxlength="64" placeholder="mindestens 4 Zeichen"></label>
      <button type="button" id="gen">Passwort würfeln</button>
      <button class="primary" type="submit">Benutzer anlegen</button>
    </form>
    <div class="msg" id="msg"></div>
  </section>

  <section>
    <h2>So verbindest du ein Gerät</h2>
    <dl>
      <dt>Server</dt><dd class="mono" id="hint-server">127.0.0.1, Port 5060 (UDP)</dd>
      <dt>Domain / Realm</dt><dd class="mono" id="hint-realm">sip.local</dd>
      <dt>Benutzer, Passwort</dt><dd>aus der Liste oben, Anmeldung per Digest</dd>
      <dt>Anrufen</dt><dd class="mono">sip:&lt;benutzer&gt;@sip.local</dd>
      <dt>Ton</dt><dd>läuft direkt zwischen den Geräten, der Server sieht ihn nicht</dd>
    </dl>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
const shown = new Set();

function rows(el, list, cols, none) {
  el.innerHTML = list.length ? list.map((r) => `<tr>${cols(r)}</tr>`).join("") : `<tr><td class="empty" colspan="5">${none}</td></tr>`;
}

function render(s) {
  $("banner").innerHTML = s.error ? `<div class="banner">${esc(s.error)}</div>` : "";
  if (s.server) {
    const h = Math.floor(s.server.uptime / 3600), m = Math.floor((s.server.uptime % 3600) / 60);
    $("meta").textContent = `${s.server.version} · läuft seit ${h} h ${m} min`;
    $("hint-realm").textContent = s.server.realm;
  }
  rows($("regs"), s.registrations, (r) =>
    `<td><strong>${esc(r.user)}</strong></td><td>${esc(r.agent || "–")}</td><td class="mono">${esc(r.address.replace(/^sip:/, ""))}</td><td>${r.expires} s</td>`,
    "Niemand angemeldet.");
  rows($("calls"), s.calls, (c) => {
    const active = c.state === 3 || c.state === 4;
    return `<td>${esc(c.from.replace(/^sip:/, ""))}</td><td>${esc(c.to.replace(/^sip:/, ""))}</td><td>${active ? mmss(c.duration) : "–"}</td>` +
      `<td><span class="pill ${active ? "ok" : "warn"}">${active ? "im Gespräch" : "klingelt"}</span></td>`;
  }, "Kein Gespräch.");
  rows($("users"), s.users, (u) =>
    `<td><strong>${esc(u.name)}</strong></td>` +
    `<td><button class="pw mono" data-pw="${esc(u.name)}" title="Klicken zum Anzeigen">${shown.has(u.name) ? esc(u.password) : "••••••••"}</button></td>` +
    `<td><span class="pill ${u.online ? "ok" : "off"}">${u.online ? "angemeldet" : "offline"}</span></td>` +
    `<td><button class="danger" data-del="${esc(u.name)}">Löschen</button></td>`,
    "Noch kein Benutzer.");
  window.__users = Object.fromEntries(s.users.map((u) => [u.name, u.password]));
}

async function refresh() {
  try { render(await (await fetch("/api/state")).json()); }
  catch (e) { render({ error: "Admin-Seite erreicht den Server nicht.", registrations: [], calls: [], users: [] }); }
}

document.addEventListener("click", async (e) => {
  const pw = e.target.closest("[data-pw]"), del = e.target.closest("[data-del]");
  if (pw) { const n = pw.dataset.pw; shown.has(n) ? shown.delete(n) : shown.add(n); pw.textContent = shown.has(n) ? window.__users[n] : "••••••••"; }
  if (del && confirm(`Benutzer „${del.dataset.del}“ löschen?`)) {
    await fetch(`/api/users/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" });
    refresh();
  }
});

$("gen").addEventListener("click", () => {
  const abc = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  $("add").elements.password.value = Array.from(bytes, (b) => abc[b % abc.length]).join("");
});

$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const r = await fetch("/api/users", { method: "POST", body: JSON.stringify({ name: f.name.value, password: f.password.value }) });
  const j = await r.json();
  $("msg").className = "msg" + (r.ok ? "" : " bad");
  $("msg").textContent = r.ok ? `Benutzer „${f.name.value}“ ist angelegt.` : j.error;
  if (r.ok) { e.target.reset(); refresh(); }
});

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"admin page on :{PORT}, rpc {RPC_URL}, users {USERS_FILE}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
