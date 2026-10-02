// The KnowItAll2 server's admin page: set up, sign in, add and remove agents, and keep the server healthy.
// Plain JavaScript with no build step. Text from the server is only ever set as text, never as HTML.
"use strict";

const main = document.getElementById("main");
const state = { form: null, refresh: null };
const AGENT_NAMES = { "claude-code": "Claude Code", codex: "Codex" };

// --- Helpers ---

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, "");
    else node.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.form) headers["X-KnowItAll2-Form"] = state.form;
  const response = await fetch(`/admin/api/${path}`, {
    method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* not JSON */ }
  if (!response.ok) {
    const error = new Error(data.error || `The server answered ${response.status}.`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3500);
}

function show(...nodes) {
  stopRefresh();
  main.replaceChildren(...nodes);
  const first = main.querySelector("input");
  (first || main).focus();
}

function sentence(text) {
  const value = String(text || "").trim();
  if (!value) return "";
  return value.charAt(0).toUpperCase() + value.slice(1) + (/[.!?]$/.test(value) ? "" : ".");
}

function errorBox() {
  return el("p", { class: "error", role: "alert", hidden: true });
}

function fail(box, error) {
  if (error.status === 401 && state.form) { start(); return; }
  box.textContent = sentence(error.message);
  box.hidden = false;
}

function field(label, name, type = "text", extra = {}) {
  return el("label", {}, label, el("input", { type, name, required: true, ...extra }));
}

function values(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function when(iso) {
  if (!iso) return "never";
  const moment = new Date(iso);
  const seconds = Math.round((Date.now() - moment.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return moment.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function size(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function codeBox(code, fresh = false) {
  const input = el("input", { type: "text", readonly: true, value: code, "aria-label": "Code",
                              class: fresh ? "fresh" : null, onclick: (event) => event.target.select() });
  const copy = el("button", { type: "button", class: "button quiet", onclick: async () => {
    input.select();
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(code); toast("Copied."); return; } catch (_) { /* fall through */ }
    }
    toast("Selected. Press Ctrl+C to copy.");
  } }, "Copy");
  return el("div", { class: "code-box" }, input, copy);
}

function samePassword(form) {
  const data = values(form);
  if (data.password !== data.confirm) throw new Error("the two passwords are not the same");
  return data;
}

// --- Signing in ---

function setupView() {
  const box = errorBox();
  const form = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    try {
      const data = samePassword(form);
      const answer = await api("POST", "setup", { username: data.username, new_password: data.password });
      state.form = answer.form;
      recoveryView(answer.recovery_code, "Your admin account is ready.");
    } catch (error) { fail(box, error); }
  } },
  field("Username", "username", "text", { autocomplete: "username", maxlength: 64 }),
  field("Password", "password", "password", { autocomplete: "new-password", minlength: 10 }),
  field("Password again", "confirm", "password", { autocomplete: "new-password", minlength: 10 }),
  el("p", { class: "hint" }, "At least 10 characters. A password manager can make one for you."),
  box,
  el("button", { class: "button", type: "submit" }, "Create admin account"));
  show(el("section", { class: "card narrow" },
    el("header", {}, el("h1", {}, "Set up your server"),
      el("p", { class: "lead" }, "You're the first to open this page, so you'll create its admin account. "
        + "Only this account can connect agents and look after the server.")),
    form));
}

function recoveryView(code, heading) {
  const done = el("button", { class: "button", type: "button", disabled: true, onclick: () => start() }, "Continue");
  const check = el("input", { type: "checkbox", onchange: (event) => { done.disabled = !event.target.checked; } });
  show(el("section", { class: "card narrow" },
    el("header", {}, el("h1", {}, "Save your recovery code"),
      el("p", { class: "lead" }, `${heading} If you ever forget your password, this code lets you set a new one. `
        + "It is shown only now, and it works once.")),
    codeBox(code, true),
    el("p", { class: "hint" }, "Keep it somewhere safe, such as your password manager."),
    el("label", { class: "check" }, check, "I've saved my recovery code"),
    done));
}

function signInView() {
  const box = errorBox();
  const form = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    try {
      const answer = await api("POST", "sign-in", values(form));
      state.form = answer.form;
      start();
    } catch (error) { fail(box, error); }
  } },
  field("Username", "username", "text", { autocomplete: "username" }),
  field("Password", "password", "password", { autocomplete: "current-password" }),
  box,
  el("button", { class: "button", type: "submit" }, "Sign in"));
  show(el("section", { class: "card narrow" },
    el("header", {}, el("h1", {}, "Sign in")),
    form,
    el("p", {}, el("button", { type: "button", class: "link", onclick: recoverView }, "Forgot your password?"))));
}

function recoverView() {
  const box = errorBox();
  const form = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    try {
      const data = samePassword(form);
      const answer = await api("POST", "recover", {
        username: data.username, recovery_code: data.code, new_password: data.password });
      state.form = answer.form;
      recoveryView(answer.recovery_code, "Your new password is set, and your old recovery code no longer works.");
    } catch (error) { fail(box, error); }
  } },
  field("Username", "username", "text", { autocomplete: "username" }),
  field("Recovery code", "code", "text", { autocomplete: "off", spellcheck: "false" }),
  field("New password", "password", "password", { autocomplete: "new-password", minlength: 10 }),
  field("New password again", "confirm", "password", { autocomplete: "new-password", minlength: 10 }),
  box,
  el("button", { class: "button", type: "submit" }, "Set new password"));
  show(el("section", { class: "card narrow" },
    el("header", {}, el("h1", {}, "Set a new password"),
      el("p", { class: "lead" }, "Use the recovery code you saved when you set up the server.")),
    form,
    el("p", { class: "hint" }, "Lost the code too? Add KNOWITALL2_RESET_ADMIN to the server's compose file and "
      + "restart it; the server's guide explains how. Memories and connected agents are kept."),
    el("p", {}, el("button", { type: "button", class: "link", onclick: signInView }, "Back to sign in"))));
}

// --- The server ---

function agentsCard(data) {
  const box = errorBox();
  const fresh = el("div", { hidden: true });
  const form = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    box.hidden = true;
    try {
      const made = await api("POST", "agents/add", values(form));
      form.reset();
      fresh.replaceChildren(
        el("p", {}, `Join code for ${made.name}:`),
        codeBox(made.code, true),
        el("p", { class: "hint" }, `Give it to the agent that is installing KnowItAll2. It works once, until `
          + `${clock(made.expires_at)} (15 minutes).`));
      fresh.hidden = false;
      refresh(false);
    } catch (error) { fail(box, error); }
  } },
  el("div", { class: "row" },
    field("Name", "name", "text", { placeholder: "For example: Workstation - Codex", maxlength: 80 }),
    el("button", { class: "button", type: "submit" }, "Add an agent")),
  box);

  const codes = data.codes.length ? el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Waiting to be used"), el("th", {}, "Works until"), el("th", {}))),
    el("tbody", {}, data.codes.map((code) => el("tr", {},
      el("td", {}, code.name), el("td", {}, clock(code.expires_at)),
      el("td", { class: "actions" }, el("button", { type: "button", class: "button danger", onclick: async () => {
        try { await api("POST", "codes/cancel", { id: code.id }); toast("Code cancelled."); refresh(false); }
        catch (error) { fail(box, error); }
      } }, "Cancel"))))))) : null;

  const agents = data.agents.length ? el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Name", "Agent", "Computer", "Version", "Last checked in", ""].map(
      (title) => el("th", {}, title)))),
    el("tbody", {}, data.agents.map((agent) => el("tr", {},
      el("td", {}, agent.name),
      el("td", {}, AGENT_NAMES[agent.agent] || agent.agent || "—"),
      el("td", {}, agent.computer || "—"),
      el("td", {}, agent.version || "—"),
      el("td", { title: agent.last_seen_at || "" }, when(agent.last_seen_at)),
      el("td", { class: "actions" }, el("button", { type: "button", class: "button danger", onclick: async () => {
        if (!confirm(`Remove ${agent.name}? Its key stops working at once. The memories it saved stay.`)) return;
        try { await api("POST", "agents/remove", { id: agent.id }); toast(`${agent.name} was removed.`); refresh(false); }
        catch (error) { fail(box, error); }
      } }, "Remove"))))))) : el("p", { class: "empty" }, "No agents are connected yet.");

  return el("section", { class: "card" },
    el("header", {}, el("h2", {}, "Agents"),
      el("p", { class: "lead" }, "Each coding agent connects with its own one-time code. Claude Code and Codex on "
        + "the same computer each need one.")),
    form, fresh, el("div", { id: "codes" }, codes), el("div", { id: "agents" }, agents));
}

function serverCard(data) {
  const server = data.server;
  const backup = server.last_backup ? when(server.last_backup)
    : "Not yet; the first is made a few minutes after the server starts";
  return el("section", { class: "card" },
    el("header", {}, el("h2", {}, "Server")),
    el("div", { class: "facts" },
      el("div", { class: "fact" }, el("span", {}, "Memories"), el("strong", {}, server.memories.toLocaleString())),
      el("div", { class: "fact" }, el("span", {}, "Database"), el("strong", {}, size(server.database_bytes))),
      el("div", { class: "fact" }, el("span", {}, "Last daily backup"), el("strong", {}, backup)),
      el("div", { class: "fact" }, el("span", {}, "Version"), el("strong", {}, server.version))),
    el("p", { class: "hint" }, "The server keeps its last seven daily backups in its data folder. Copy them off "
      + "the server as part of your own backups, or download one now."),
    el("p", {}, el("a", { class: "button quiet", href: "/admin/api/backup", download: "" }, "Download a backup")));
}

function accountCard() {
  const passwordBox = errorBox();
  const passwordForm = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    passwordBox.hidden = true;
    try {
      const data = samePassword(passwordForm);
      await api("POST", "password", { current: data.current, new_password: data.password });
      passwordForm.reset();
      toast("Password changed. Other sign-ins were signed out.");
    } catch (error) { fail(passwordBox, error); }
  } },
  el("h3", {}, "Change password"),
  field("Current password", "current", "password", { autocomplete: "current-password" }),
  field("New password", "password", "password", { autocomplete: "new-password", minlength: 10 }),
  field("New password again", "confirm", "password", { autocomplete: "new-password", minlength: 10 }),
  passwordBox,
  el("div", {}, el("button", { class: "button quiet", type: "submit" }, "Change password")));

  const codeBoxError = errorBox();
  const recovery = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    try {
      const answer = await api("POST", "recovery-code", values(recovery));
      recoveryView(answer.recovery_code, "Your old recovery code no longer works.");
    } catch (error) { fail(codeBoxError, error); }
  } },
  el("h3", {}, "New recovery code"),
  el("p", { class: "hint" }, "Lost your recovery code, or think someone saw it? Make a new one; the old one stops working."),
  field("Password", "password", "password", { autocomplete: "current-password" }),
  codeBoxError,
  el("div", {}, el("button", { class: "button quiet", type: "submit" }, "Make a new recovery code")));

  return el("section", { class: "card" }, el("header", {}, el("h2", {}, "Account")), passwordForm, recovery);
}

function overviewView(data) {
  const notice = data.reset_reminder ? el("div", { class: "notice", role: "note" },
    "The server was started with ", el("code", {}, "KNOWITALL2_RESET_ADMIN"), " in its compose file. "
    + "Empty that setting and restart the server, so it is not left in place.") : null;
  show(...[notice, agentsCard(data), serverCard(data), accountCard()].filter(Boolean));
  startRefresh();
}

async function refresh(whole) {
  try {
    const data = await api("GET", "overview");
    if (whole) { overviewView(data); return; }
    const card = agentsCard(data);
    document.getElementById("codes")?.replaceWith(card.querySelector("#codes"));
    document.getElementById("agents")?.replaceWith(card.querySelector("#agents"));
  } catch (error) {
    if (error.status === 401) start();
  }
}

function startRefresh() {
  stopRefresh();
  state.refresh = setInterval(() => { if (!document.hidden) refresh(false); }, 30000);
}

function stopRefresh() {
  if (state.refresh) clearInterval(state.refresh);
  state.refresh = null;
}

// --- Start ---

async function start() {
  try {
    const current = await api("GET", "state");
    document.getElementById("version").textContent = `KnowItAll2 ${current.version}`;
    const who = document.getElementById("who");
    who.hidden = !current.signed_in;
    document.getElementById("who-name").textContent = current.username || "";
    state.form = current.form || null;
    if (current.setup_needed) setupView();
    else if (!current.signed_in) signInView();
    else refresh(true);
  } catch (error) {
    show(el("section", { class: "card narrow" }, el("h1", {}, "The server did not answer"),
      el("p", { class: "lead" }, sentence(error.message)),
      el("div", {}, el("button", { class: "button", type: "button", onclick: start }, "Try again"))));
  }
}

document.getElementById("sign-out").addEventListener("click", async () => {
  try { await api("POST", "sign-out", {}); } catch (_) { /* signed out either way */ }
  state.form = null;
  start();
});

start();
