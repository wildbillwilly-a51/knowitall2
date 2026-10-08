/* KnowItAll2 app: plain JavaScript, no build step. Every view reads the live
   database through the local server; nothing here is precomputed. Words on
   screen are for someone who is not a developer; technical detail sits
   behind "Show details". */
'use strict';

const REFRESH_MS = 5000;
const HEARTBEAT_MS = 30000;

const state = {
  key: null,
  window: Math.random().toString(36).slice(2) + Date.now().toString(36),
  tz: new Date().getTimezoneOffset(),
  view: null,
  refresh: null,
  busy: false,
  online: null,
  expanded: new Set(),
  period: 'today',
};

/* ---------- The key, safely ---------- */

function storageGet(name) {
  try { return window.sessionStorage.getItem(name); } catch (e) { return null; }
}
function storageSet(name, value) {
  try { window.sessionStorage.setItem(name, value); } catch (e) { /* this window only */ }
}

(function readKey() {
  const found = /[#&]key=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (found) {
    state.key = found[1];
    storageSet('knowitall2-key', found[1]);
    history.replaceState(null, '', location.pathname + '#/overview');
  } else {
    state.key = storageGet('knowitall2-key');
  }
  try {
    const saved = window.localStorage.getItem('knowitall2-period');
    if (saved === 'today' || saved === 'week') state.period = saved;
  } catch (e) { /* default */ }
})();

/* ---------- HTML that is always escaped ---------- */

class Raw { constructor(text) { this.text = text; } toString() { return this.text; } }
function raw(text) { return new Raw(text); }
function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function part(value) {
  if (value instanceof Raw) return value.text;
  if (Array.isArray(value)) return value.map(part).join('');
  if (value === null || value === undefined || value === false) return '';
  return esc(value);
}
function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, index) => { out += part(value) + strings[index + 1]; });
  return raw(out);
}

/* ---------- Talking to the server ---------- */

class AppError extends Error {}

async function api(path, options = {}) {
  const headers = { 'X-KnowItAll2-Key': state.key || '', 'X-KnowItAll2-Window': state.window };
  const init = { method: options.method || 'GET', headers, cache: 'no-store' };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (e) {
    setOnline(false);
    throw new AppError('KnowItAll2 is not responding. It stops when its window closes; open it again from its shortcut.');
  }
  setOnline(true);
  let data = {};
  try { data = await response.json(); } catch (e) { data = {}; }
  if (response.status === 401) {
    showLocked();
    throw new AppError(data.error || 'This window has no key.');
  }
  if (!response.ok) throw new AppError(data.error || `KnowItAll2 answered ${response.status}.`);
  return data;
}

function withTz(path) {
  return path + (path.includes('?') ? '&' : '?') + 'tz=' + encodeURIComponent(state.tz);
}

function setOnline(online) {
  if (state.online === online) return;
  state.online = online;
  const element = document.getElementById('connection');
  element.className = 'connection ' + (online ? 'online' : 'offline');
  element.querySelector('.label').textContent = online ? 'Connected' : 'Not connected';
  const live = document.querySelector('.pill-live');
  if (live) live.classList.toggle('stale', !online);
}

function heartbeat() {
  api('/api/ping').then((data) => {
    document.getElementById('version').textContent = 'Version ' + data.version;
  }).catch(() => {});
}

window.addEventListener('pagehide', () => {
  if (!state.key || !navigator.sendBeacon) return;
  navigator.sendBeacon('/api/bye', new Blob([state.key + ' ' + state.window], { type: 'text/plain' }));
});

/* ---------- Words: plain labels for what KnowItAll2 stores ---------- */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const KIND_WORDS = {
  fact: 'Fact', procedure: 'How-to', decision: 'Decision', lesson: 'Lesson learned', rule: 'Your rule', note: 'Note',
};
const VERIFICATION_WORDS = { user_stated: 'You said so', observed: 'Seen in action', unverified: 'Not checked yet' };
const VERIFICATION_HELP = {
  user_stated: 'Your own words. It does not fade, and only you can change it.',
  observed: 'An agent saw this in the output of something it ran.',
  unverified: 'An agent concluded this without seeing proof. Agents treat it as a lead to check.',
};
const ACTIVITY_WORDS = {
  briefing: 'Session start', recall: 'Search', remember: 'Saved', forget: 'Forgot', restore: 'Brought back',
  confirm: 'Confirmed', question: 'Question', answer: 'Your answer', settle: 'Settled', task: 'Request to agents',
  settings: 'Settings', learning: 'Learning run', candidate: 'Idea', change: 'Tidied up', maintenance: 'Tidy-up',
  catalog: 'Organizing', move: 'Moved',
};
const OUTCOME_WORDS = {
  shown: 'gave background', empty: 'nothing to give', found: 'found something', nothing: 'found nothing',
  saved: 'saved', updated: 'updated', 'already known': 'already known', 'saved with a question': 'saved, with a question',
  rejected: 'turned down', retired: 'forgotten', restored: 'brought back', confirmed: 'confirmed', asked: 'asked',
  ok: 'done', 'nothing new': 'nothing new', stopped: 'stopped', 'partly failed': 'partly failed',
  'waiting for budget': 'waiting for its daily limit', 'merged duplicate': 'merged a duplicate',
  'replaced outdated': 'replaced an outdated one', 'retired snapshot': 'removed a temporary note',
  'learning on': 'learning turned on', 'learning off': 'learning turned off', limits: 'limits changed',
  check: 'asked to check', 'find out': 'asked to find out', 'catch up': 'catching up', shortcut: 'shortcut',
  use_new: 'newer one kept', keep_mine: 'older one kept', keep_both: 'both kept', make_rule: 'made a rule',
  keep_note: 'kept as a note', forget: 'forgotten', keep: 'kept', 'nothing to review': 'nothing to review',
  moved: 'moved to another project',
};
const REASON_WORDS = {
  'evidence not in the session': "the agent couldn't show where it saw this",
  length: 'too short or too long', 'secret': 'it contained a password or key', kind: "unclear what kind of note it was",
  scope: 'unclear where it applies', 'not accepted by memory': "it couldn't be saved",
};
const TONES = {
  ok: ['saved', 'found', 'shown', 'ok', 'restored', 'updated', 'learning on', 'confirmed', 'use_new', 'keep_mine',
    'keep_both', 'make_rule', 'keep', 'moved'],
  accent: ['saved with a question', 'asked', 'merged duplicate', 'replaced outdated', 'retired snapshot', 'check',
    'find out', 'catch up'],
  bad: ['rejected', 'stopped', 'failed', 'partly failed'],
  warn: ['retired', 'learning off', 'waiting for budget', 'forget'],
};
const STATUS_TONES = { ready: 'ok', partial: 'warn', mentioned: '', project: 'teal', practice: 'teal', other: '' };

function tone(outcome) {
  for (const [name, outcomes] of Object.entries(TONES)) {
    if (outcomes.includes(outcome)) return name;
  }
  return '';
}
function outcomeChip(outcome) {
  return outcome ? html`<span class="chip ${tone(outcome)}">${OUTCOME_WORDS[outcome] || outcome}</span>` : '';
}
function kindWord(kind) { return KIND_WORDS[kind] || kind; }
function verificationChip(verification) {
  const shade = verification === 'user_stated' ? 'ok' : verification === 'observed' ? 'accent' : '';
  return html`<span class="chip ${shade}" title="${VERIFICATION_HELP[verification] || ''}">${VERIFICATION_WORDS[verification] || verification}</span>`;
}
function scopeWord(item) {
  return item.scope === 'project' ? `Only in ${item.project_name || 'one project'}` : 'Everywhere';
}
function reasonWord(reason) { return REASON_WORDS[reason] || reason; }
// A learning run counts outcomes such as "rejected (length)"; say them plainly.
function outcomeWords(name) {
  const rejected = /^rejected \((.*)\)$/.exec(name);
  return rejected ? `turned down, ${reasonWord(rejected[1])}` : (OUTCOME_WORDS[name] || name);
}
function headlineOf(item) { return item.headline || item.text; }

/* ---------- Formatting ---------- */

function clock(date) {
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function when(iso) {
  if (!iso) return 'never';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 45) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return seconds < 6 * 3600 ? `${Math.floor(seconds / 3600)} h ago` : `today ${clock(date)}`;
  if (date.toDateString() === yesterday.toDateString()) return `yesterday ${clock(date)}`;
  const year = date.getFullYear() === today.getFullYear() ? '' : ` ${date.getFullYear()}`;
  return `${MONTHS[date.getMonth()]} ${date.getDate()}${year}`;
}
function fullTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}
function timeTag(iso, className = '') {
  return html`<time class="${className}" datetime="${iso}" title="${fullTime(iso)}">${when(iso)}</time>`;
}
function number(value) { return Number(value || 0).toLocaleString(); }
function tokens(value) {
  const count = Number(value || 0);
  if (count >= 1e6) return (count / 1e6).toFixed(1) + ' million';
  if (count >= 1e4) return Math.round(count / 1e3) + ' thousand';
  return count.toLocaleString();
}
function percent(part, whole) { return whole ? Math.round((part * 100) / whole) + '%' : '—'; }
function plural(count, one, many) { return `${number(count)} ${count === 1 ? one : (many || one + 's')}`; }

function toast(message, bad = false) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.className = 'toast' + (bad ? ' bad' : '');
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, bad ? 7000 : 3500);
}

/* ---------- Views and routing ---------- */

const main = () => document.getElementById('main');

function setMain(content) {
  main().innerHTML = part(content);
  applyWidths(main());
  bindToggles(main());
}

// The page's policy forbids inline style attributes; widths are set from script.
function applyWidths(root) {
  root.querySelectorAll('[data-width]').forEach((element) => {
    element.style.width = Math.max(0, Math.min(100, Number(element.dataset.width) || 0)) + '%';
  });
}

// "Show details" links reveal the element named in data-toggle.
function bindToggles(root) {
  root.querySelectorAll('[data-toggle]:not([data-bound])').forEach((button) => {
    button.dataset.bound = '1';
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const target = document.getElementById(button.dataset.toggle);
      if (!target) return;
      target.hidden = !target.hidden;
      button.textContent = target.hidden ? (button.dataset.show || 'Show details') : (button.dataset.hide || 'Hide details');
    });
  });
}

function details(id, content, { show = 'Show details', hide = 'Hide details' } = {}) {
  return html`<button type="button" class="link-button" data-toggle="${id}" data-show="${show}" data-hide="${hide}">${show}</button>
    <div id="${id}" class="details-box" hidden>${content}</div>`;
}

function showLocked() {
  state.refresh = null;
  setMain(html`
    <div class="locked">
      <img src="icon.svg" alt="" width="56" height="56">
      <h1>Open KnowItAll2 from its shortcut</h1>
      <p>This window lost its connection key, or the app was restarted. Close it and open KnowItAll2 again from the Start menu or the desktop.</p>
    </div>`);
}

function showError(error) {
  setMain(html`<div class="notice bad">${error.message || String(error)}</div>`);
}

const VIEWS = {};

function currentRoute() {
  const [path, query] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || 'overview', args: parts.slice(1), params: new URLSearchParams(query || '') };
}

async function route() {
  const { name, args, params } = currentRoute();
  const view = VIEWS[name] ? name : 'overview';
  document.querySelectorAll('.nav a').forEach((link) => link.classList.toggle('active', link.dataset.view === view));
  state.view = view;
  state.refresh = null;
  state.expanded.clear();
  try {
    await VIEWS[view](args, params);
    main().focus({ preventScroll: true });
  } catch (error) {
    if (!(error instanceof AppError && /key/.test(error.message))) showError(error);
  }
}

window.addEventListener('hashchange', route);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') tick(); });

async function tick() {
  if (!state.refresh || state.busy || document.visibilityState !== 'visible') return;
  state.busy = true;
  try { await state.refresh(); } catch (e) { /* the connection indicator says it */ } finally { state.busy = false; }
}

setInterval(tick, REFRESH_MS);
setInterval(heartbeat, HEARTBEAT_MS);

function refreshTimes() {
  document.querySelectorAll('time[datetime]').forEach((element) => { element.textContent = when(element.getAttribute('datetime')); });
}

function updateQuestionCount(count) {
  const badge = document.getElementById('question-count');
  badge.hidden = !count;
  badge.textContent = count ? String(count) : '';
}

function pageHead(title, sentence, { live = false, back = null } = {}) {
  return html`<header class="page-head">
      <div>${back ? html`<p class="back"><a href="${back[0]}">← ${back[1]}</a></p>` : ''}<h1>${title}</h1>${sentence ? html`<p>${sentence}</p>` : ''}</div>
      ${live ? html`<span class="pill-live" title="This page updates by itself"><span class="dot"></span>Live</span>` : ''}
    </header>`;
}

/* ---------- Overview ---------- */

VIEWS.overview = async () => {
  let last = '';
  const load = async () => {
    const data = await api(withTz('/api/overview'));
    const signature = JSON.stringify(data, (key, value) => (key === 'now' ? undefined : value));
    updateQuestionCount(data.questions);
    if (signature === last) { refreshTimes(); return; }
    last = signature;
    setMain(renderOverview(data));
    bindOverview();
  };
  await load();
  state.refresh = load;
};

function summarySentence(data) {
  const week = data.periods.week;
  const parts = [];
  if (week.briefings) parts.push(`gave your agents background ${plural(week.briefings, 'time')}`);
  if (week.recalls) parts.push(`answered ${plural(week.recalls, 'search', 'searches')}`);
  if (week.learned) parts.push(`learned ${plural(week.learned, 'new thing')}`);
  if (!parts.length) return 'KnowItAll2 has not been used yet this week.';
  const last = parts.pop();
  return `This week KnowItAll2 ${parts.length ? parts.join(', ') + ', and ' : ''}${last}.`;
}

function sharingLine(sharing) {
  const synced = sharing.last_success ? `last synced ${when(sharing.last_success)}` : 'not synced yet';
  const waiting = sharing.unsent ? '; some changes made here are waiting to be sent' : '';
  return `Memory is shared through the KnowItAll2 server at ${sharing.address}: ${synced}${waiting}.`;
}

function renderOverview(data) {
  const period = data.periods[state.period];
  const health = data.health;
  return html`
    ${pageHead('Overview', summarySentence(data), { live: true })}
    <section class="card health ${health.level}">
      <span class="badge-dot"></span>
      <div>
        <h2>${health.headline}</h2>
        ${health.items.length ? html`<ul>${health.items.map((item) => html`
          <li class="${item.level}"><span>${item.text}${item.action && item.action.route ? html`<a href="${item.action.route}">${item.action.label}</a>` : ''}
            ${item.action && item.action.hint ? html`<span class="hint">${item.action.hint}</span>` : ''}</span></li>`)}</ul>`
          : html`<p class="muted">Your agents are getting background at the start of their sessions, learning is running, and nothing has gone wrong recently.</p>`}
        ${data.sharing ? html`<p class="muted small mt-8">${sharingLine(data.sharing)}</p>` : ''}
        ${data.questions_in_progress ? html`<p class="muted small mt-8">${plural(data.questions_in_progress, 'question is', 'questions are')} being looked into by KnowItAll2 or an agent. <a href="#/questions">See them</a></p>` : ''}
      </div>
    </section>
    <div class="period">
      <h3>${state.period === 'today' ? 'Today' : 'The last 7 days'}</h3>
      <div class="segmented" role="group" aria-label="Period">
        <button type="button" data-period="today" class="${state.period === 'today' ? 'on' : ''}">Today</button>
        <button type="button" data-period="week" class="${state.period === 'week' ? 'on' : ''}">Last 7 days</button>
      </div>
    </div>
    <div class="tiles">
      ${tile('Session starts', period.briefings, 'agents given background', '#/activity?group=use')}
      ${tile('Searches', period.recalls, period.recalls ? `${percent(period.recall_hits, period.recalls)} found something` : 'agents looking something up', '#/activity?group=use')}
      ${tile('Learned', period.learned, 'new things', '#/learning')}
      ${tile('Turned down', period.rejected, 'ideas that did not pass the checks', '#/learning')}
      ${tile('Saved by agents', period.saved, 'things agents chose to remember', '#/activity?group=changes')}
      ${tile('Tidied up', period.changes, 'duplicates and outdated notes', '#/activity?group=learning')}
    </div>
    <div class="grid-2">
      <section class="card">
        <h3>The last 14 days</h3>
        ${chart(data.daily)}
      </section>
      <section class="card">${learningCard(data.learning, period)}</section>
    </div>
    <div class="grid-2 mt-16">
      <section class="card">${knowsCard(data.memories)}</section>
      <section class="card">${agentsCard(data.agents)}</section>
    </div>
    ${data.problems.length ? html`
      <section class="card mt-16">
        <h3>Recent problems <a class="aside" href="#/activity?group=problems">All problems</a></h3>
        <ul class="list compact">${data.problems.map(problemRow)}</ul>
      </section>` : ''}
    <section class="card mt-16">
      <h3>Recent activity <a class="aside" href="#/activity">All activity</a></h3>
      ${data.recent.length ? html`<ul class="list compact">${data.recent.map((event) => eventRow(event, { compact: true }))}</ul>`
        : html`<div class="empty">Activity appears here as your agents use KnowItAll2.</div>`}
    </section>`;
}

function bindOverview() {
  main().querySelectorAll('[data-period]').forEach((button) => {
    button.addEventListener('click', () => {
      state.period = button.dataset.period;
      try { window.localStorage.setItem('knowitall2-period', state.period); } catch (e) { /* this session only */ }
      route();
    });
  });
  const doctor = main().querySelector('[data-action="doctor"]');
  if (doctor) doctor.addEventListener('click', runDoctor);
}

function tile(label, value, sub, link) {
  return html`<a class="tile" href="${link}"><div class="label">${label}</div><div class="value">${number(value)}</div><div class="sub">${sub}</div></a>`;
}

function chart(days) {
  const width = 560; const height = 190; const left = 26; const bottom = 18; const gap = 4;
  const top1 = 8; const h1 = 84; const top2 = top1 + h1 + 16; const h2 = height - top2 - bottom;
  const slot = (width - left) / days.length;
  const added = days.map((day) => day.learner + day.agent + day.user);
  const used = days.map((day) => day.briefings + day.recalls);
  const maxAdded = Math.max(1, ...added);
  const maxUsed = Math.max(1, ...used);
  const bars = [];
  days.forEach((day, index) => {
    const x = left + index * slot + gap;
    const barWidth = (slot - 2 * gap).toFixed(1);
    let y = top1 + h1;
    for (const origin of ['learner', 'agent', 'user']) {
      const value = day[origin];
      if (!value) continue;
      const size = (value / maxAdded) * h1;
      y -= size;
      bars.push(`<rect class="${origin}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth}" height="${size.toFixed(1)}" rx="2"><title>${esc(day.day)}: ${value} ${origin === 'learner' ? 'learned' : origin === 'agent' ? 'saved by agents' : 'from you'}</title></rect>`);
    }
    const usedHeight = (used[index] / maxUsed) * h2;
    const hitHeight = ((day.briefings + day.recall_hits) / maxUsed) * h2;
    bars.push(`<rect class="use" x="${x.toFixed(1)}" y="${(top2 + h2 - usedHeight).toFixed(1)}" width="${barWidth}" height="${usedHeight.toFixed(1)}" rx="2"><title>${esc(day.day)}: ${day.briefings} session starts, ${day.recalls} searches</title></rect>`);
    bars.push(`<rect class="use-hit" x="${x.toFixed(1)}" y="${(top2 + h2 - hitHeight).toFixed(1)}" width="${barWidth}" height="${hitHeight.toFixed(1)}" rx="2"><title>${esc(day.day)}: ${day.briefings + day.recall_hits} times it had something to give</title></rect>`);
    if (index % 2 === days.length % 2 || index === days.length - 1) {
      const date = new Date(day.day + 'T12:00:00');
      bars.push(`<text x="${(x + (slot - 2 * gap) / 2).toFixed(1)}" y="${height - 4}" text-anchor="middle">${date.getDate()}</text>`);
    }
  });
  const imported = days.reduce((sum, day) => sum + day.import, 0);
  return html`
    <p class="muted small">Top: what KnowItAll2 took in each day. Bottom: how often your agents used it.</p>
    <svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="What KnowItAll2 took in and how often agents used it, per day">
      <text x="0" y="${top1 + 8}">${maxAdded}</text>
      <line class="axis" x1="${left}" x2="${width}" y1="${top1 + h1}" y2="${top1 + h1}"></line>
      <text x="0" y="${top2 + 8}">${maxUsed}</text>
      <line class="axis" x1="${left}" x2="${width}" y1="${top2 + h2}" y2="${top2 + h2}"></line>
      ${raw(bars.join(''))}
    </svg>
    <div class="legend">
      <span><i class="learner"></i>learned</span><span><i class="agent"></i>saved by agents</span><span><i class="user"></i>from you</span>
      <span><i class="use-hit"></i>used, and it helped</span><span><i class="use"></i>used</span>
    </div>
    ${imported ? html`<p class="faint small mt-6">Not shown: ${plural(imported, 'thing')} brought in from another system.</p>` : ''}`;
}

function learningCard(learning, period) {
  const last = learning.last_run;
  const used = Math.min(100, Math.round((learning.calls_today * 100) / Math.max(1, learning.calls_per_day)));
  return html`
    <h3>Learning <a class="aside" href="#/learning">Details</a></h3>
    <p>
      ${learning.enabled ? html`<span class="chip ok">On</span>` : html`<span class="chip warn">Off</span>`}
      ${learning.running && !learning.now ? html`<span class="chip accent">Learning right now</span>` : ''}
      <span class="muted">using your ${learning.engine} sign-in</span>
      ${learning.engine_found ? '' : html`<span class="chip bad">${learning.engine} not found</span>`}
    </p>
    ${nowLine(learning.now)}
    ${learning.latest ? html`<p>${timeTag(learning.latest.at)}: ${newsSentence(learning.latest)}</p>`
      : last ? html`<p>Last ran ${timeTag(last.at)}. <span class="muted">${runSentence(last)}</span></p>`
      : html`<p class="muted">It has not run yet. It learns when you commit, when a session ends, or when you ask your agent to.</p>`}
    <div class="stat-line"><span>Model calls in the last 24 hours</span><span class="v">${number(learning.calls_today)} of ${number(learning.calls_per_day)} allowed</span></div>
    <div class="bar ${used >= 90 ? 'warn' : ''} bar-gap"><span data-width="${used}"></span></div>
    <p class="faint small mt-8">Each call sends a cleaned-up excerpt of a finished session to your own ${learning.engine} account. Nothing is sent anywhere else.</p>`;
}

/* What learning is doing, and what each run learned: the same news the agents show in the session. */

function nowLine(now) {
  if (!now) return '';
  return html`<p class="now-line"><span class="chip accent">Learning right now</span>
    ${now.doing}${now.folder ? ` in ${now.folder}` : ''}${now.why ? ` (${now.why})` : ''}
    <span class="faint small">started ${timeTag(now.since)}</span></p>`;
}

function newsSentence(item) {
  const where = item.folders.length ? ` in ${item.folders.join(', ')}` : '';
  const what = item.reason === 'catch-up'
    ? `${plural(Math.max(1, item.sessions), 'earlier session')} that ended without being learned` : `a session${where}`;
  const because = item.why ? ` (${item.why})` : '';
  if (item.status === 'limit') {
    return `Not learned yet from ${what}${because}: today's limit${item.limit ? ` of ${number(item.limit)} calls` : ''} is used up.`;
  }
  if (item.status === 'stopped') return `Could not learn from ${what}${because}: ${item.problem || 'the model was not available'}.`;
  const count = item.saved.length + item.updated.length;
  if (!count) return `Checked ${what}${because}: nothing new to remember.`;
  return `Learned ${plural(count, 'thing')} from ${what}${because}.`;
}

function newsItem(item) {
  const learned = [...item.saved, ...item.updated];
  const extras = [];
  if (item.already_known) extras.push(`${number(item.already_known)} already known`);
  if (item.turned_down.length) extras.push(`${number(item.turned_down.length)} turned down`);
  if (item.questions) extras.push(`${plural(item.questions, 'question')} for you`);
  const turnedDown = html`<ul class="plain-list">${item.turned_down.map((idea) => html`
    <li>${idea.text || 'An idea that held a secret; nothing of it was kept.'} <span class="faint">(${reasonWord(idea.reason)})</span></li>`)}</ul>`;
  return html`<li class="news-item">
    <div>${timeTag(item.at)} ${outcomeChip(NEWS_OUTCOMES[item.status] || '')} <span>${newsSentence(item)}</span></div>
    ${learned.length ? html`<ul class="plain-list mt-6">${learned.map((memory) => html`
      <li>${memory.id ? html`<a href="#/memories/${memory.id}">${memory.text}</a>` : memory.text}</li>`)}</ul>` : ''}
    ${extras.length ? html`<p class="faint small mt-6">${extras.join(', ')}.
      ${item.turned_down.length ? details(`down-${item.id}`, turnedDown, { show: 'Why turned down', hide: 'Hide' }) : ''}</p>` : ''}
    ${item.partial ? html`<p class="faint small">${item.reason === 'catch-up' ? "The rest of that session waits for today's limit."
      : 'The rest of that session is learned at its next commit or end.'}</p>` : ''}
    ${item.can_learn_anyway ? html`<div class="btn-row mt-8">
      <button type="button" class="btn small" data-anyway="${item.id}">Learn anyway</button>
      <span class="faint small">${item.status === 'limit' ? "Learns this session now, even though today's limit is used up."
        : 'Learns the rest of this session now.'}</span></div>` : ''}
  </li>`;
}
const NEWS_OUTCOMES = { limit: 'waiting for budget', stopped: 'stopped' };

function learningNews(data) {
  return html`
    <h3>What it just learned</h3>
    ${nowLine(data.now)}
    ${data.news.length ? html`<ul class="news-list">${data.news.map(newsItem)}</ul>`
      : html`<p class="muted">Nothing yet. What KnowItAll2 learns shows here, and in the session itself when a turn ends.</p>`}`;
}

function runSentence(run) {
  const d = run.details || {};
  const outcomes = d.outcomes || {};
  const kept = ['saved', 'updated', 'saved with a question'].reduce((sum, name) => sum + (outcomes[name] || 0), 0);
  const sessions = (d.sessions || []).length;
  if (run.outcome === 'stopped') return 'It stopped because of a problem.';
  if (!sessions) return 'There was nothing new to learn from.';
  if (!d.calls && d.deferred) return `${plural(sessions, 'session is', 'sessions are')} waiting for its daily limit.`;
  return `It read ${plural(sessions, 'session')} and learned ${plural(kept, 'new thing')}.`;
}

function knowsCard(memories) {
  const kinds = Object.entries(memories.by_kind).sort((a, b) => b[1] - a[1]);
  const top = Math.max(1, ...kinds.map(([, count]) => count));
  const verification = memories.by_verification;
  return html`
    <h3>What it knows <a class="aside" href="#/knowledge">See it all</a></h3>
    <p><span class="big-number">${number(memories.active)}</span> <span class="muted">things, about ${plural(memories.systems || 0, 'system')} and project${memories.systems === 1 ? '' : 's'}</span></p>
    ${memories.unfiled ? html`<p class="faint small">${plural(memories.unfiled, 'new thing is', 'new things are')} still being sorted.</p>` : ''}
    <div class="kinds">${kinds.map(([kind, count]) => html`
      <div class="row"><span>${kindWord(kind)}</span><div class="bar"><span data-width="${Math.round((count * 100) / top)}"></span></div><span class="n">${number(count)}</span></div>`)}</div>
    <div class="mt-12">
      <div class="stat-line"><span>You said so</span><span class="v">${number(verification.user_stated || 0)}</span></div>
      <div class="stat-line"><span>Seen in action</span><span class="v">${number(verification.observed || 0)}</span></div>
      <div class="stat-line"><span>Not checked yet</span><span class="v">${number(verification.unverified || 0)}</span></div>
      <div class="stat-line"><span>Not used by an agent yet</span><span class="v">${number(memories.never_used)}</span></div>
    </div>`;
}

function agentsCard(agents) {
  return html`
    <h3>Your agents <button type="button" class="btn small" data-action="doctor">Check everything</button></h3>
    ${agents.map((agent) => html`
      <div class="agent-row">
        <div>
          <strong>${agent.name}</strong>
          ${agent.installed && !agent.ok ? html`<ul class="checks">${agent.checks.filter((check) => !check.ok).map((check) => html`
            <li><span class="bad">✗</span><span>${check.name}: ${check.detail}${check.fix ? html`<br><span class="faint">${check.fix}</span>` : ''}</span></li>`)}</ul>` : ''}
        </div>
        ${!agent.installed ? html`<span class="chip">not installed</span>`
          : agent.ok ? html`<span class="chip ok">connected</span>` : html`<span class="chip bad">needs attention</span>`}
      </div>`)}
    <div id="doctor-result"></div>`;
}

async function runDoctor(event) {
  const button = event.currentTarget;
  const target = document.getElementById('doctor-result');
  button.disabled = true;
  button.textContent = 'Checking…';
  const pause = state.refresh;
  state.refresh = null;
  try {
    const result = await api('/api/doctor', { method: 'POST', body: {} });
    target.innerHTML = part(html`
      <p class="mt-10"><strong>${result.ok ? 'Everything checks out.' : 'Some checks failed.'}</strong></p>
      <ul class="checks">${result.checks.map((check) => html`
        <li><span class="${!check.ok ? 'bad' : check.fix ? 'warn' : 'ok'}">${!check.ok ? '✗' : check.fix ? '!' : '✓'}</span><span>${check.name}: ${check.detail}${check.fix ? html`<br><span class="faint">${check.fix}</span>` : ''}</span></li>`)}</ul>`);
  } catch (error) {
    target.innerHTML = part(html`<p class="notice bad mt-10">${error.message}</p>`);
  } finally {
    button.disabled = false;
    button.textContent = 'Check everything';
    setTimeout(() => { if (state.view === 'overview' && !state.refresh) state.refresh = pause; }, 20000);
  }
}

/* ---------- What it knows ---------- */

VIEWS.knowledge = async (args) => {
  if (args[0]) {
    await systemProfile(args[0]);
    return;
  }
  const data = await api('/api/knowledge');
  const systems = data.areas.flatMap((area) => area.systems);
  const ready = systems.filter((item) => item.status.key === 'ready').length;
  const partial = systems.filter((item) => item.status.key === 'partial').length;
  setMain(html`
    ${pageHead('What KnowItAll2 knows', `${plural(systems.length, 'system and project', 'systems and projects')}. ${ready} ready to use, ${partial} partly known.`)}
    ${data.unfiled ? html`<div class="notice info">${plural(data.unfiled, 'new thing is', 'new things are')} still being sorted in the background.</div>` : ''}
    <div class="toolbar"><input class="input grow" type="search" id="system-filter" placeholder="Find a system" aria-label="Find a system"></div>
    ${data.areas.length ? data.areas.map((area) => html`
      <section class="area" data-area>
        <h2 class="area-title">${area.area}</h2>
        <div class="system-grid">${area.systems.map((system) => html`
          <a class="system-card" href="#/knowledge/${system.id}" data-name="${system.name.toLowerCase()}">
            <div class="system-top"><strong>${system.name}</strong><span class="chip ${STATUS_TONES[system.status.key] || ''}">${system.status.label}</span></div>
            <p class="muted small">${system.summary || 'A summary is being written.'}</p>
            <p class="faint small">${plural(system.memories, 'thing')} known${system.missing ? ` · ${plural(system.missing, 'gap')}` : ''}</p>
          </a>`)}</div>
      </section>`) : html`<section class="card"><div class="empty"><h3>Nothing sorted yet</h3>KnowItAll2 sorts what it knows by system in the background, after learning.</div></section>`}
    ${data.general ? html`<p class="muted mt-16">${plural(data.general, 'thing is', 'things are')} not about any one system. <a href="#/memories?system=none">See them</a></p>` : ''}`);
  const filter = document.getElementById('system-filter');
  filter.addEventListener('input', () => {
    const wanted = filter.value.trim().toLowerCase();
    main().querySelectorAll('.system-card').forEach((card) => { card.hidden = wanted && !card.dataset.name.includes(wanted); });
    main().querySelectorAll('[data-area]').forEach((area) => { area.hidden = !area.querySelector('.system-card:not([hidden])'); });
  });
};

async function systemProfile(id, { keep = null } = {}) {
  const data = await api('/api/knowledge/' + encodeURIComponent(id));
  const facets = data.facets;
  const missing = data.missing;
  setMain(html`
    ${pageHead(data.name, data.summary || '', { back: ['#/knowledge', 'What it knows'] })}
    <section class="card">
      <div class="profile-top">
        <span class="chip ${STATUS_TONES[data.status.key] || ''}">${data.status.label}</span>
        <span class="muted small">${plural(data.memories, 'thing')} known${data.aliases.length ? ` · also called ${data.aliases.join(', ')}` : ''}</span>
        ${data.last_seen_working ? html`<span class="muted small">· last seen working ${timeTag(data.last_seen_working)}</span>` : ''}
      </div>
      <dl class="profile">
        ${facets.map((facet) => html`
          <dt>${facet.label}</dt>
          <dd><ul class="plain-list">${facet.memories.map((item, index) => html`
            <li>${headlineOf(item)} ${details(`m-${item.id}-${index}`, memoryDetails(item), { show: 'details' })}</li>`)}</ul></dd>`)}
        ${missing.map((item) => html`<dt>${item.label}</dt><dd class="unknown">Not known</dd>`)}
        ${data.elsewhere.length ? html`
          <dt>Filed under something else, and names it</dt>
          <dd><ul class="plain-list">${data.elsewhere.map((item, index) => html`
            <li>${headlineOf(item)} ${details(`e-${item.id}-${index}`, memoryDetails(item), { show: 'details' })}</li>`)}</ul></dd>` : ''}
      </dl>
    </section>
    ${(missing.length || data.gaps.length || data.finding) ? html`
      <section class="card mt-16" id="missing-card">${missingCard(data)}</section>` : ''}
    <section class="card mt-16">
      <h3>Tell KnowItAll2 something about ${data.name}</h3>
      <div class="form-grid">
        <label for="tell-facet">Which part</label>
        <select class="input" id="tell-facet">${Object.entries(data.facet_labels).map(([key, label]) => html`<option value="${key}">${label}</option>`)}</select>
        <label for="tell-text">What to remember</label>
        <textarea class="input" id="tell-text" maxlength="2000" placeholder="For example: the sign-in is in Vaultwarden, in the item named vcenter-admin. Never type the password itself."></textarea>
      </div>
      <div class="btn-row mt-8"><button type="button" class="btn primary" id="tell-save">Save</button><span class="faint small">Saved as your own words.</span></div>
    </section>
    <p class="mt-16"><a href="#/memories?system=${data.id}">All ${plural(data.memories, 'thing')} about ${data.name}, one by one</a></p>`);
  if (keep) {
    // A refresh after a search finished: keep what the user was typing.
    document.getElementById('tell-facet').value = keep.facet;
    document.getElementById('tell-text').value = keep.text;
  }
  bindSystemActions(data);
  const looking = data.finding && data.finding.status === 'looking';
  state.refresh = looking ? () => refreshFinding(id) : null;
}

// While an agent looks, only the status line updates; when it is done, the whole profile does.
async function refreshFinding(id) {
  const data = await api('/api/knowledge/' + encodeURIComponent(id));
  if (data.finding && data.finding.status === 'looking') {
    const line = document.getElementById('finding-status');
    if (line) line.innerHTML = part(findingResult(data.finding));
    return;
  }
  const keep = { facet: document.getElementById('tell-facet').value, text: document.getElementById('tell-text').value };
  await systemProfile(id, { keep });
  toast(data.finding && data.finding.status === 'done' ? 'The agent finished looking.' : 'The search stopped.');
}

function missingCard(data) {
  const labels = [...data.missing.map((item) => item.label), ...data.gaps];
  const openRequests = data.requests.filter((item) => item.status === 'open');
  const finding = data.finding;
  const looking = finding && finding.status === 'looking';
  return html`
    <h3>What's missing</h3>
    ${labels.length ? html`<ul class="gap-list">${labels.map((label) => html`<li><span>${label}</span></li>`)}</ul>`
      : html`<p class="muted">Nothing is missing now.</p>`}
    ${labels.length ? html`<div class="btn-row mt-12">
      <button type="button" class="btn primary" id="find-out" ${looking ? raw('disabled') : ''}>Ask an agent to find these out</button>
      <span class="faint small">${data.search.folder
        ? `One agent reads the files in ${data.search.folder}, using your ${data.search.engine} sign-in, and changes nothing. It saves only what it can show in a file.`
        : `No project folder for ${data.name} is known on this computer, so agents that work with it will be asked instead.`}</span>
    </div>` : ''}
    <div id="finding-status">${findingResult(finding)}</div>
    ${openRequests.length && !looking ? html`<p class="muted small mt-8">Agents working with ${data.name} have been asked to find out: ${openRequests.map((item) => item.prompt.replace(/^KnowItAll2 does not know (.*?)\. If.*$/, '$1')).join('; ')}.</p>` : ''}
    ${labels.length ? html`<p class="faint small mt-8">If you know one of these, tell KnowItAll2 below.</p>` : ''}`;
}

function findingResult(finding) {
  if (!finding) return '';
  if (finding.status === 'looking') {
    return html`<p class="now-line mt-8"><span class="chip accent">Looking now</span>
      An agent started looking ${timeTag(finding.started_at)}. This page shows what it finds when it is done.</p>`;
  }
  if (finding.status === 'failed') return html`<div class="notice mt-12">${finding.message || 'The search stopped.'}</div>`;
  const found = finding.found || [];
  const notFound = finding.not_found || [];
  const turnedDown = finding.turned_down || [];
  return html`
    <div class="mt-12">
      <p><strong>Last search</strong> ${timeTag(finding.finished_at)}${finding.folder ? ` in ${finding.folder}` : ''}:
        ${found.length ? `found ${plural(found.length, 'thing')}.` : 'found nothing new.'} ${finding.message || ''}</p>
      ${found.length ? html`<ul class="plain-list mt-6">${found.map((item) => html`
        <li><strong>${item.label}:</strong> ${item.id ? html`<a href="#/memories/${item.id}">${item.text}</a>` : item.text}
          <span class="faint small">(from ${item.file})</span></li>`)}</ul>` : ''}
      ${turnedDown.length ? html`<p class="faint small mt-6">${plural(turnedDown.length, 'answer was', 'answers were')} not kept: ${turnedDown.map((item) => item.reason).join('; ')}.</p>` : ''}
      ${notFound.length && finding.folder ? html`<p class="faint small mt-6">Not in the files: ${notFound.join('; ')}. Agents that work with it will be asked.</p>` : ''}
    </div>`;
}

function bindSystemActions(data) {
  const facet = document.getElementById('tell-facet');
  const text = document.getElementById('tell-text');
  const find = document.getElementById('find-out');
  if (find) {
    find.addEventListener('click', async () => {
      find.disabled = true;
      try {
        const result = await api(`/api/knowledge/${data.id}/find-out`, { method: 'POST', body: {} });
        toast(result.message);
        await systemProfile(data.id, { keep: { facet: facet.value, text: text.value } });
      } catch (error) {
        toast(error.message, true);
        find.disabled = false;
      }
    });
  }
  document.getElementById('tell-save').addEventListener('click', async (click) => {
    const button = click.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/knowledge/${data.id}/tell`, { method: 'POST', body: { facet: facet.value, text: text.value } });
      toast(result.message);
      route();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  });
}

function memoryDetails(item) {
  return html`
    <p class="exact">${item.text}</p>
    <p class="meta">${kindWord(item.kind)} · ${verificationChip(item.verification)} · ${scopeWord(item)}
      · learned ${when(item.created_at)}${item.source_agent ? ` via ${item.source_agent}` : ''}
      · <a href="#/memories/${item.id}">open</a></p>`;
}

/* ---------- Memories ---------- */

const MEMORY_FILTERS = ['q', 'system', 'kind', 'verification', 'origin', 'status', 'sort', 'project'];
const ORIGINS = { user: 'you', learner: 'learning', agent: 'agents', import: 'another system' };
const SORTS = { recent: 'Recently confirmed', changed: 'Recently changed', used: 'Most used', unused: 'Never used', oldest: 'Oldest first' };

VIEWS.memories = async (args, params) => {
  if (args[0]) {
    await memoryDetail(args[0]);
    return;
  }
  const filters = Object.fromEntries(MEMORY_FILTERS.map((name) => [name, params.get(name) || '']));
  const known = await api('/api/knowledge');
  setMain(memoriesShell(filters, known));
  const list = document.getElementById('memory-list');
  const summary = document.getElementById('memory-summary');
  const more = document.getElementById('memory-more');
  let offset = 0;
  let generation = 0;

  const load = async (append = false) => {
    const mine = ++generation;
    const query = new URLSearchParams();
    MEMORY_FILTERS.forEach((name) => { if (filters[name]) query.set(name, filters[name]); });
    if (append) query.set('offset', String(offset));
    const data = await api('/api/memories?' + query);
    if (mine !== generation) return;
    offset = data.offset + data.items.length;
    const rows = part(data.items.map(memoryRow));
    if (append) list.insertAdjacentHTML('beforeend', rows);
    else list.innerHTML = rows || part(html`<li><div class="empty">Nothing matches.</div></li>`);
    bindToggles(list);
    summary.textContent = `${number(data.total)} ${data.total === 1 ? 'thing' : 'things'}${filters.q ? ' match' : ''}`;
    more.hidden = offset >= data.total;
    const shown = new URLSearchParams(query);
    shown.delete('offset');
    history.replaceState(null, '', '#/memories' + (shown.toString() ? '?' + shown : ''));
  };

  let timer = null;
  main().querySelectorAll('[data-filter]').forEach((control) => {
    const update = () => {
      filters[control.dataset.filter] = control.value;
      clearTimeout(timer);
      timer = setTimeout(() => load().catch((error) => toast(error.message, true)), control.tagName === 'INPUT' ? 250 : 0);
    };
    control.addEventListener(control.tagName === 'INPUT' ? 'input' : 'change', update);
  });
  more.querySelector('button').addEventListener('click', () => load(true).catch((error) => toast(error.message, true)));
  await load();
};

function memoriesShell(filters, known) {
  const option = (value, label, current) => html`<option value="${value}" ${value === current ? raw('selected') : ''}>${label}</option>`;
  const systems = known.areas.flatMap((area) => area.systems).sort((a, b) => a.name.localeCompare(b.name));
  return html`
    ${pageHead('Memories', 'Each thing KnowItAll2 knows, in a sentence. Looking here never counts as an agent using it.')}
    <div class="toolbar">
      <input class="input grow" type="search" placeholder="Search" data-filter="q" value="${filters.q}" aria-label="Search">
      <select class="input" data-filter="system" aria-label="System">
        ${option('', 'Every system', filters.system)}
        ${systems.map((system) => option(system.id, system.name, filters.system))}
        ${option('none', 'Not about any one system', filters.system)}
      </select>
      <select class="input" data-filter="kind" aria-label="Kind">
        ${option('', 'Any kind', filters.kind)}${Object.entries(KIND_WORDS).map(([key, label]) => option(key, label, filters.kind))}
      </select>
      <select class="input" data-filter="verification" aria-label="How sure">
        ${option('', 'However sure', filters.verification)}
        ${Object.entries(VERIFICATION_WORDS).map(([key, label]) => option(key, label, filters.verification))}
      </select>
      <select class="input" data-filter="origin" aria-label="Where it came from">
        ${option('', 'From anywhere', filters.origin)}
        ${Object.entries(ORIGINS).map(([key, label]) => option(key, `From ${label}`, filters.origin))}
      </select>
      <select class="input" data-filter="status" aria-label="Status">
        ${option('', 'Current', filters.status)}${option('inactive', 'Forgotten or replaced', filters.status)}
      </select>
      <select class="input" data-filter="sort" aria-label="Order">
        ${Object.entries(SORTS).map(([key, label]) => option(key === 'recent' ? '' : key, label, filters.sort))}
      </select>
    </div>
    <section class="card">
      <p class="muted small" id="memory-summary">Loading…</p>
      <ul class="list" id="memory-list"></ul>
      <div class="more" id="memory-more" hidden><button type="button" class="btn">Show more</button></div>
    </section>`;
}

function memoryRow(item) {
  return html`
    <li class="memory-row">
      <a class="text" href="#/memories/${item.id}">${headlineOf(item)}</a>
      <span class="meta">
        ${item.system_name ? html`<a class="chip teal" href="#/knowledge/${item.system_id}">${item.system_name}</a>` : ''}
        <span class="chip">${kindWord(item.kind)}</span>${verificationChip(item.verification)}
        ${item.status !== 'active' ? html`<span class="chip warn">${item.status === 'superseded' ? 'replaced' : 'forgotten'}</span>` : ''}
        <span>${item.recall_count ? `used ${plural(item.recall_count, 'time')}` : 'not used yet'}</span>
        ${details(`d-${item.id}`, memoryDetails(item), { show: 'details' })}
      </span>
    </li>`;
}

async function memoryDetail(id) {
  const data = await api('/api/memories/' + encodeURIComponent(id));
  const item = data.memory;
  const uses = item.uses || {};
  const active = item.status === 'active';
  setMain(html`
    ${pageHead(headlineOf(item), '', { back: ['#/memories', 'Memories'] })}
    <section class="card">
      <p class="muted small">Exact wording</p>
      <div class="memory-text">${item.text}</div>
      <div class="btn-row" id="memory-actions">
        ${active && item.verification !== 'user_stated' ? html`<button type="button" class="btn" data-act="confirm" title="It becomes your own words: it will not fade, and only you can change it">This is right</button>` : ''}
        ${active ? html`<button type="button" class="btn" data-act="correct">Correct it…</button>` : ''}
        ${active ? html`<button type="button" class="btn danger" data-act="forget">Forget it…</button>` : ''}
        ${!active ? html`<button type="button" class="btn primary" data-act="restore">Bring it back</button>` : ''}
      </div>
      <div id="memory-form"></div>
    </section>
    ${data.questions.length ? html`<div class="notice info mt-16">There is an open question about this. <a href="#/questions">See it</a></div>` : ''}
    <div class="grid-2 mt-16">
      <section class="card">
        <h3>About it</h3>
        <dl class="kv">
          <dt>Kind</dt><dd>${kindWord(item.kind)}</dd>
          ${item.system_name ? html`<dt>About</dt><dd><a href="#/knowledge/${item.system_id}">${item.system_name}</a>${item.facet_label ? ` (${item.facet_label.toLowerCase()})` : ''}</dd>` : ''}
          <dt>Applies</dt><dd>${scopeWord(item)}</dd>
          <dt>How sure</dt><dd>${verificationChip(item.verification)} <span class="muted small">${VERIFICATION_HELP[item.verification] || ''}</span></dd>
          <dt>Status</dt><dd>${active ? 'Current' : item.status === 'superseded' ? 'Replaced by a newer version' : 'Forgotten'}${item.retired_reason ? html`<br><span class="muted">${item.retired_reason}</span>` : ''}</dd>
          ${data.replaced_by ? html`<dt>Replaced by</dt><dd><a href="#/memories/${data.replaced_by.id}">${data.replaced_by.text}</a></dd>` : ''}
          ${item.replaced.length ? html`<dt>It replaced</dt><dd>${item.replaced.map((old) => html`<a href="#/memories/${old.id}">${old.text}</a><br>`)}</dd>` : ''}
        </dl>
      </section>
      <section class="card">
        <h3>Where it came from, and its use</h3>
        <dl class="kv">
          <dt>Learned</dt><dd>${fullTime(item.created_at)}${item.source_agent ? ` via ${item.source_agent}` : ''}${item.source_kind === 'user' ? ' (your words)' : ''}</dd>
          ${item.confirmed_at && item.confirmed_at !== item.created_at ? html`<dt>Last confirmed</dt><dd>${fullTime(item.confirmed_at)}</dd>` : ''}
          <dt>Used</dt><dd>${item.recall_count ? `${plural(item.recall_count, 'time')}: ${plural(uses.briefing || 0, 'session start')}, ${plural(uses.recall || 0, 'search', 'searches')}` : 'not given to an agent yet'}</dd>
          ${item.last_used_at ? html`<dt>Last used</dt><dd>${fullTime(item.last_used_at)}</dd>` : ''}
        </dl>
        ${details('tech-' + item.id, html`<dl class="kv"><dt>Id</dt><dd class="mono">${item.id}</dd>
          ${item.source_session ? html`<dt>Session</dt><dd class="mono">${item.source_session}</dd>` : ''}
          ${item.subjects.length ? html`<dt>Labels</dt><dd>${item.subjects.join(', ')}</dd>` : ''}</dl>`, { show: 'Technical details' })}
      </section>
    </div>
    <section class="card mt-16">
      <h3>History</h3>
      ${data.events.length ? html`<ul class="list" id="events">${data.events.map((event) => eventRow(event))}</ul>`
        : html`<div class="empty">Nothing has happened to it yet.</div>`}
    </section>`);
  const list = document.getElementById('events');
  if (list) bindEvents(list);
  bindMemoryActions(item);
}

function bindMemoryActions(item) {
  const form = document.getElementById('memory-form');
  const act = async (action, body = {}) => {
    const result = await api(`/api/memories/${encodeURIComponent(item.id)}/${action}`, { method: 'POST', body });
    toast(result.message);
    if (result.id && result.id !== item.id) location.hash = '#/memories/' + result.id;
    else route();
  };
  document.querySelectorAll('#memory-actions [data-act]').forEach((button) => {
    button.addEventListener('click', async () => {
      const action = button.dataset.act;
      if (action === 'confirm' || action === 'restore') {
        button.disabled = true;
        try { await act(action); } catch (error) { toast(error.message, true); button.disabled = false; }
        return;
      }
      if (action === 'correct') {
        form.innerHTML = part(html`
          <div class="mt-12"><label class="muted small" for="correction">Your wording replaces this and counts as your own words.</label>
          <textarea class="input mt-6" id="correction" maxlength="2000">${item.text}</textarea>
          <div class="btn-row mt-8"><button type="button" class="btn primary" data-go>Save</button><button type="button" class="btn" data-cancel>Cancel</button></div></div>`);
      } else {
        form.innerHTML = part(html`
          <div class="mt-12"><label class="muted small" for="reason">Agents will stop seeing it. You can bring it back later.</label>
          <input class="input mt-6 wide" id="reason" placeholder="Why (optional)" maxlength="200">
          <div class="btn-row mt-8"><button type="button" class="btn danger" data-go>Forget it</button><button type="button" class="btn" data-cancel>Cancel</button></div></div>`);
      }
      const field = form.querySelector('textarea, input');
      field.focus();
      form.querySelector('[data-cancel]').addEventListener('click', () => { form.innerHTML = ''; });
      form.querySelector('[data-go]').addEventListener('click', async (click) => {
        click.currentTarget.disabled = true;
        try {
          await act(action, action === 'correct' ? { text: field.value } : { reason: field.value });
        } catch (error) {
          toast(error.message, true);
          click.currentTarget.disabled = false;
        }
      });
    });
  });
}

/* ---------- Questions ---------- */

VIEWS.questions = async () => {
  let last = '';
  const load = async () => {
    const data = await api('/api/questions');
    updateQuestionCount(data.for_you.length);
    const signature = JSON.stringify(data);
    if (signature === last) { refreshTimes(); return; }
    last = signature;
    setMain(questionsPage(data));
    bindQuestions();
  };
  await load();
  state.refresh = load;
};

function questionsPage(data) {
  return html`
    ${pageHead('Questions', 'KnowItAll2 settles what it can, then asks an agent to check. You only see what is still unclear, or what is yours to decide.', { live: true })}
    ${data.for_you.length ? html`<div class="stack">${data.for_you.map(questionCard)}</div>`
      : html`<section class="card"><div class="empty"><h3>Nothing needs you</h3>${data.in_progress.length ? 'The questions below are being looked into.' : 'When something is unclear and only you can settle it, it shows up here.'}</div></section>`}
    ${data.in_progress.length ? html`
      <section class="card mt-16">
        <h3>Being looked into</h3>
        <ul class="list">${data.in_progress.map((item) => html`
          <li class="event">${timeTag(item.created_at, 'time')}
            <div class="body"><div class="summary">${item.plain}</div>
              <div class="meta"><span class="chip accent">${stageWords(item)}</span></div></div></li>`)}</ul>
      </section>` : ''}
    ${data.answered.length ? html`
      <section class="card mt-16">
        <h3>Recently settled</h3>
        <ul class="list">${data.answered.map((item) => html`
          <li class="event">${timeTag(item.answered_at, 'time')}
            <div class="body"><div class="summary">${item.prompt}</div>
              <div class="meta"><span class="chip ok">${item.answer_label}</span><span class="faint small">settled by ${item.settled_by}</span></div></div></li>`)}</ul>
      </section>` : ''}`;
}

function stageWords(item) {
  if (item.stage === 'checking') return 'Waiting for an agent to check it';
  return 'KnowItAll2 will look at it in its next background run';
}

const QUESTION_TITLES = {
  conflict: 'Two notes disagree',
  confirm_rule: 'Is this one of your rules?',
  still_true: 'Is this still true?',
};

function questionCard(question) {
  const choices = question.labels;
  return html`
    <section class="card question" data-question="${question.id}">
      <div class="line"><span class="kind">${QUESTION_TITLES[question.kind] || 'Question'}</span> <span class="faint small">asked ${when(question.created_at)}</span></div>
      <p class="prompt">${question.plain}</p>
      ${question.context ? html`<p class="faint small">${question.context}</p>` : ''}
      ${question.reason ? html`<p class="muted small">${question.reason}</p>` : ''}
      <div class="options">
        ${choices.map((option) => html`<button type="button" class="btn" data-choice="${option.key}">${option.label}</button>`)}
        <button type="button" class="btn quiet" data-choice="not_sure">Not sure: leave things as they are</button>
      </div>
      ${details('q-' + question.id, html`
        <div class="${question.memories.length > 1 ? 'grid-2' : ''}">
          ${question.memories.map((memory, index) => html`
            <div class="card inner">
              ${question.kind === 'conflict' ? html`<p class="muted small">${index === 0 ? 'The older note' : 'The newer note'}</p>` : ''}
              <p>${headlineOf(memory)}</p>
              <p class="exact">${memory.text}</p>
              <div class="meta">${verificationChip(memory.verification)}
                <span class="faint small">learned ${when(memory.created_at)}${memory.source_agent ? ` via ${memory.source_agent}` : ''}</span>
                <a class="small" href="#/memories/${memory.id}">open</a></div>
            </div>`)}
        </div>
        ${question.findings.length ? html`<p class="muted small mt-8">What was found so far:</p><ul class="plain-list small">${question.findings.map((finding) => html`<li>${finding}</li>`)}</ul>` : ''}`)}
    </section>`;
}

function bindQuestions() {
  main().querySelectorAll('[data-question]').forEach((card) => {
    card.querySelectorAll('[data-choice]').forEach((button) => {
      button.addEventListener('click', async () => {
        card.querySelectorAll('button').forEach((other) => { other.disabled = true; });
        try {
          const result = await api(`/api/questions/${encodeURIComponent(card.dataset.question)}/answer`, {
            method: 'POST', body: { choice: button.dataset.choice },
          });
          toast('Thanks. ' + (button.dataset.choice === 'not_sure' ? 'Left as it is.' : 'Done.'));
          updateQuestionCount(result.remaining);
          route();
        } catch (error) {
          toast(error.message, true);
          card.querySelectorAll('button').forEach((other) => { other.disabled = false; });
        }
      });
    });
  });
}

/* ---------- Learning ---------- */

VIEWS.learning = async () => {
  let last = '';
  let openRun = null;
  const load = async () => {
    const data = await api('/api/learning');
    const signature = JSON.stringify([data.runs, data.running, data.waiting, data.calls_today, data.week, data.now,
      data.news]);
    if (signature === last) { refreshTimes(); return; }
    const firstRender = !last;
    last = signature;
    if (firstRender) {
      setMain(learningPage(data));
      bindLearningSettings(data);
      loadCatchUp();
    } else {
      const news = document.getElementById('learning-news');
      news.innerHTML = part(learningNews(data));
      bindToggles(news);
      document.getElementById('learning-status').innerHTML = part(learningStatus(data));
      document.getElementById('learning-week').innerHTML = part(learningWeek(data.week));
      applyWidths(document.getElementById('learning-week'));
      document.getElementById('learning-runs').innerHTML = part(learningRuns(data.runs));
    }
    bindLearningLive(() => openRun, (run) => { openRun = run; });
    if (openRun) await showRun(openRun);
  };
  await load();
  state.refresh = load;
};

function learningPage(data) {
  return html`
    ${pageHead('Learning', 'KnowItAll2 learns as you work: when you commit, when a session ends, or when you ask your agent to.', { live: true })}
    <section class="card" id="learning-news">${learningNews(data)}</section>
    <div class="grid-2 mt-16">
      <section class="card" id="learning-status">${learningStatus(data)}</section>
      <section class="card">
        <h3>How it works</h3>
        <p class="muted">When a turn ends with a commit, when a Claude Code session ends, or when you ask your agent to learn, KnowItAll2 sends a cleaned-up excerpt of the session (passwords and keys removed) to your own ${data.settings.backend === 'codex-cli' ? 'Codex' : 'Claude Code'} account and asks what is worth remembering. It runs on its own, so your agent never waits, and it tells you in the session what it learned. Sessions that ended without any of those are read a while after they go quiet.</p>
        <p class="muted">It keeps an idea only if the agent can point to where it saw it, and only your own words become rules. Afterwards it tidies up: merging duplicates, replacing outdated notes, and sorting everything by system.</p>
      </section>
    </div>
    <section class="card mt-16" id="learning-week">${learningWeek(data.week)}</section>
    <section class="card mt-16"><h3>Recent runs <span class="aside">choose one to see every idea it considered</span></h3><div id="learning-runs">${learningRuns(data.runs)}</div><div id="run-detail"></div></section>
    <section class="card mt-16" id="catch-up"><h3>Past sessions not learned yet</h3><p class="muted">Looking…</p></section>
    <section class="card mt-16">${learningSettings(data)}</section>`;
}

function learningStatus(data) {
  const settings = data.settings;
  const engine = data.engines[settings.backend] || { name: settings.backend, found: false };
  const waiting = data.waiting || {};
  const lastRun = data.runs[0];
  const used = Math.round((data.calls_today * 100) / Math.max(1, settings.max_calls_per_day));
  return html`
    <h3>Right now</h3>
    <p>${settings.enabled ? html`<span class="chip ok">On</span>` : html`<span class="chip warn">Off</span>`}
      ${data.running ? html`<span class="chip accent">Learning right now</span>` : ''}
      <span class="muted">using your ${engine.name} sign-in</span>
      ${engine.found ? '' : html`<span class="chip bad">${engine.name} not found on this computer</span>`}</p>
    <div class="stat-line"><span>Last run</span><span class="v">${lastRun ? html`${timeTag(lastRun.at)} ${outcomeChip(lastRun.outcome)}` : 'not yet'}</span></div>
    <div class="stat-line"><span>Sessions ready to read</span><span class="v">${waiting.error ? html`<span class="faint">${waiting.error}</span>` : number(waiting.ready)}</span></div>
    <div class="stat-line"><span>Sessions still going</span><span class="v">${waiting.error ? '' : number(waiting.active)}</span></div>
    <div class="stat-line"><span>Model calls in the last 24 hours</span><span class="v">${number(data.calls_today)} of ${number(settings.max_calls_per_day)} allowed</span></div>
    <div class="bar ${used >= 90 ? 'warn' : ''} bar-gap"><span data-width="${used}"></span></div>
    <div class="btn-row mt-8">
      <button type="button" class="btn primary" data-learning="run" ${!settings.enabled || data.running ? raw('disabled') : ''}>Learn now</button>
      <span class="faint small">${settings.enabled ? 'It runs in the background; this page updates as it goes.' : 'Turn learning on below first.'}</span>
    </div>`;
}

function learningWeek(week) {
  const kept = ['saved', 'updated', 'saved with a question'].reduce((sum, name) => sum + (week.candidates[name] || 0), 0);
  const rejected = week.candidates.rejected || 0;
  const known = week.candidates['already known'] || 0;
  const reasons = Object.entries(week.reasons);
  const top = Math.max(1, ...reasons.map(([, count]) => count));
  const t = week.tokens;
  return html`
    <h3>The last 7 days</h3>
    <div class="funnel">
      <div class="step"><div class="value">${number(week.sessions)}</div><div class="label">sessions read</div></div>
      <div class="step"><div class="value">${number(kept + rejected + known)}</div><div class="label">ideas considered</div></div>
      <div class="step"><div class="value">${number(kept)}</div><div class="label">kept</div></div>
      <div class="step"><div class="value">${number(known)}</div><div class="label">already known</div></div>
      <div class="step"><div class="value">${number(rejected)}</div><div class="label">turned down</div></div>
    </div>
    ${reasons.length ? html`
      <p class="muted small label-gap">Why ideas were turned down</p>
      <div class="kinds">${reasons.map(([reason, count]) => html`
        <div class="row reason"><span>${reasonWord(reason)}</span><div class="bar warn"><span data-width="${Math.round((count * 100) / top)}"></span></div><span class="n">${number(count)}</span></div>`)}</div>` : ''}
    ${details('week-usage', html`<p class="muted small">${plural(week.runs, 'run')}, ${plural(week.calls, 'learning call')}${week.review_calls ? `, ${plural(week.review_calls, 'tidy-up call')}` : ''}.
      Tokens: ${t.total_tokens ? `${tokens(t.total_tokens)} (Codex)` : ''}${t.total_tokens && t.input_tokens ? ', ' : ''}${t.input_tokens ? `${tokens(t.input_tokens)} sent and ${tokens(t.output_tokens)} received` : ''}${!t.total_tokens && !t.input_tokens ? 'none recorded yet' : ''}${t.cost_usd ? `, about $${t.cost_usd.toFixed(2)} at API prices (a subscription is not billed per call)` : ''}.</p>`, { show: 'Usage details' })}`;
}

function learningRuns(runs) {
  if (!runs.length) return html`<div class="empty">No learning run yet.</div>`;
  return html`
    <div class="table-wrap"><table class="table">
      <thead><tr><th>When</th><th>What happened</th><th class="n">Kept</th><th class="n">Turned down</th></tr></thead>
      <tbody>${runs.map((run) => {
        const d = run.details || {};
        const outcomes = d.outcomes || {};
        const kept = ['saved', 'updated', 'saved with a question'].reduce((sum, name) => sum + (outcomes[name] || 0), 0);
        const rejected = Object.entries(outcomes).filter(([name]) => name.startsWith('rejected')).reduce((sum, [, count]) => sum + count, 0);
        return html`<tr class="link" data-run="${run.run_id || ''}">
          <td class="nowrap">${timeTag(run.at)}</td><td>${runSentence(run)}</td>
          <td class="n">${number(kept)}</td><td class="n">${number(rejected)}</td></tr>`;
      })}</tbody></table></div>`;
}

function bindLearningLive(getOpen, setOpen) {
  const run = main().querySelector('[data-learning="run"]');
  if (run && !run.dataset.bound) {
    run.dataset.bound = '1';
    run.addEventListener('click', async () => {
      run.disabled = true;
      try {
        const result = await api('/api/learning/run', { method: 'POST', body: {} });
        toast(result.message);
      } catch (error) {
        toast(error.message, true);
        run.disabled = false;
      }
    });
  }
  main().querySelectorAll('[data-anyway]:not([data-bound])').forEach((button) => {
    button.dataset.bound = '1';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await api(`/api/learning/news/${button.dataset.anyway}/anyway`, { method: 'POST', body: {} });
        toast(result.message);
        if (state.refresh) state.refresh();
      } catch (error) {
        toast(error.message, true);
        button.disabled = false;
      }
    });
  });
  main().querySelectorAll('#learning-runs tr[data-run]').forEach((row) => {
    row.addEventListener('click', async () => {
      const id = row.dataset.run;
      if (!id) return;
      if (getOpen() === id) {
        setOpen(null);
        document.getElementById('run-detail').innerHTML = '';
        return;
      }
      setOpen(id);
      await showRun(id);
    });
  });
}

async function showRun(id) {
  const target = document.getElementById('run-detail');
  const data = await api('/api/activity?limit=200&run=' + encodeURIComponent(id));
  const ideas = data.events.filter((event) => event.kind === 'candidate');
  const changes = data.events.filter((event) => event.kind === 'change');
  target.innerHTML = part(html`
    <div class="mt-16">
      <h3>Ideas in this run</h3>
      ${ideas.length ? html`<ul class="list mt-8" id="run-events">${ideas.map((event) => eventRow(event))}</ul>`
        : html`<p class="muted mt-8">This run had no ideas to consider.</p>`}
      ${changes.length ? html`<p class="muted small label-gap">Tidied up</p><ul class="list">${changes.map((event) => eventRow(event))}</ul>` : ''}
    </div>`);
  const list = document.getElementById('run-events');
  if (list) bindEvents(list);
}

async function loadCatchUp() {
  const target = document.getElementById('catch-up');
  try {
    const data = await api('/api/learning/catch-up');
    const groups = data.groups.filter((group) => group.folder !== '(unknown folder)');
    target.innerHTML = part(html`
      <h3>Past sessions not learned yet</h3>
      <p class="muted">These sessions happened before KnowItAll2 was set up, or were skipped because the old system was thought to have learned them. Learning from them costs model calls, so you choose.</p>
      ${groups.length ? html`<div class="table-wrap"><table class="table">
        <thead><tr><th>Project folder</th><th class="n">Sessions</th><th>Last used</th><th></th></tr></thead>
        <tbody>${groups.slice(0, 25).map((group) => html`<tr>
          <td>${shortFolder(group.folder)}</td><td class="n">${number(group.sessions)}</td><td class="nowrap">${when(group.last_active)}</td>
          <td class="nowrap"><span class="btn-row"><button type="button" class="btn small" data-estimate="${group.folder}">How many calls?</button>
            <button type="button" class="btn small" data-catch-up="${group.folder}">Learn these</button></span></td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">Nothing is waiting.</p>`}`);
    target.querySelectorAll('[data-estimate]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.textContent = 'Counting…';
        try {
          const result = await api('/api/learning/catch-up/estimate', { method: 'POST', body: { folder: button.dataset.estimate } });
          button.textContent = `About ${number(result.calls)} calls`;
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
          button.textContent = 'How many calls?';
        }
      });
    });
    target.querySelectorAll('[data-catch-up]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (button.dataset.sure !== '1') {
          button.dataset.sure = '1';
          button.textContent = 'Sure? Click again';
          return;
        }
        button.disabled = true;
        try {
          const result = await api('/api/learning/catch-up', { method: 'POST', body: { folder: button.dataset.catchUp } });
          toast(result.message);
          loadCatchUp();
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
        }
      });
    });
  } catch (error) {
    target.innerHTML = part(html`<h3>Past sessions not learned yet</h3><p class="notice bad">${error.message}</p>`);
  }
}

function shortFolder(folder) {
  const parts = folder.split(/[\\/]/).filter(Boolean);
  return parts.length > 2 ? `…${folder.includes('\\') ? '\\' : '/'}${parts.slice(-2).join(folder.includes('\\') ? '\\' : '/')}` : folder;
}

function learningSettings(data) {
  const s = data.settings;
  const engineOption = (key) => {
    const engine = data.engines[key];
    return html`<option value="${key}" ${s.backend === key ? raw('selected') : ''}>${engine.name}${engine.found ? '' : ' (not found on this computer)'}</option>`;
  };
  return html`
    <h3>Settings</h3>
    <div class="form-grid" id="learning-form">
      <label for="learning-enabled">Learning</label>
      <label class="switch"><input type="checkbox" id="learning-enabled" ${s.enabled ? raw('checked') : ''}><span id="learning-enabled-label">${s.enabled ? 'On' : 'Off'}</span></label>
      <label for="learning-backend">Use my account on</label>
      <select class="input" id="learning-backend">${engineOption('claude-cli')}${engineOption('codex-cli')}</select>
    </div>
    ${details('learning-advanced', html`
      <div class="form-grid mt-8">
        <label for="learning-model">Model</label>
        <input class="input" id="learning-model" value="${s.model}" placeholder="the default" maxlength="80">
        <label for="learning-run">Most calls in one run</label>
        <input class="input" id="learning-run" type="number" min="1" max="50" value="${s.max_calls_per_run}">
        <label for="learning-day" title="Learning from your own work (a commit, a session's end, or when you ask) is never held back by this limit.">Most calls per day for background learning</label>
        <input class="input" id="learning-day" type="number" min="1" max="500" value="${s.max_calls_per_day}">
        <label for="learning-idle">Wait after a session goes quiet (minutes)</label>
        <input class="input" id="learning-idle" type="number" min="5" max="240" value="${s.idle_minutes}">
      </div>`, { show: 'Advanced settings', hide: 'Hide advanced settings' })}
    <p class="notice info mt-12" id="learning-consent" ${s.enabled ? raw('hidden') : ''}>Turning learning on is your OK for KnowItAll2 to send cleaned-up excerpts of your finished sessions to your own Claude Code or Codex account. Nothing is sent anywhere else.</p>
    <div class="btn-row mt-12"><button type="button" class="btn primary" id="learning-save">Save</button></div>`;
}

function bindLearningSettings(data) {
  const enabled = document.getElementById('learning-enabled');
  const backend = document.getElementById('learning-backend');
  const model = document.getElementById('learning-model');
  enabled.addEventListener('change', () => {
    document.getElementById('learning-enabled-label').textContent = enabled.checked ? 'On' : 'Off';
    document.getElementById('learning-consent').hidden = !enabled.checked || data.settings.enabled;
  });
  backend.addEventListener('change', () => {
    // Another engine has its own models; start from its default.
    model.value = backend.value === data.settings.backend ? data.settings.model : '';
  });
  document.getElementById('learning-save').addEventListener('click', async (click) => {
    const button = click.currentTarget;
    button.disabled = true;
    const whole = (id) => Number.parseInt(document.getElementById(id).value, 10);
    try {
      const result = await api('/api/learning/settings', {
        method: 'POST',
        body: {
          enabled: enabled.checked, backend: backend.value, model: model.value.trim(),
          max_calls_per_run: whole('learning-run'), max_calls_per_day: whole('learning-day'), idle_minutes: whole('learning-idle'),
        },
      });
      toast(result.message);
      route();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  });
}

/* ---------- Activity ---------- */

const GROUPS = [
  ['all', 'Everything'], ['use', 'Agents using it'], ['learning', 'Learning'], ['changes', 'Changes'], ['problems', 'Problems'],
];

VIEWS.activity = async (args, params) => {
  const group = params.get('group') || 'all';
  const run = params.get('run');
  const memory = params.get('memory');
  if (group === 'problems' && !run && !memory) {
    const data = await api('/api/problems');
    setMain(html`
      ${activityHead(group)}
      <section class="card">${data.problems.length ? html`<ul class="list">${data.problems.map(problemRow)}</ul>`
        : html`<div class="empty"><h3>No problems</h3>If something goes wrong, such as learning failing or an agent not reaching KnowItAll2, it shows up here.</div>`}</section>`);
    state.refresh = async () => {
      const fresh = await api('/api/problems');
      if (fresh.problems.length !== data.problems.length) route();
    };
    return;
  }
  const query = new URLSearchParams({ group });
  if (run) query.set('run', run);
  if (memory) query.set('memory', memory);
  const data = await api('/api/activity?' + query);
  const events = data.events;
  let next = data.next;
  setMain(html`
    ${activityHead(group, run, memory)}
    <section class="card">
      ${events.length ? html`<ul class="list" id="events">${events.map((event) => eventRow(event))}</ul>`
        : html`<div class="empty"><h3>Nothing here yet</h3>KnowItAll2 records what it does from the moment this version was installed.</div>`}
      <div class="more" id="more" ${next ? '' : 'hidden'}><button type="button" class="btn">Show older</button></div>
    </section>`);
  const list = document.getElementById('events');
  if (list) bindEvents(list);
  const more = document.getElementById('more');
  more.querySelector('button').addEventListener('click', async () => {
    const older = await api('/api/activity?' + query + '&before=' + next);
    next = older.next;
    const target = document.getElementById('events');
    target.insertAdjacentHTML('beforeend', part(older.events.map((event) => eventRow(event))));
    bindEvents(target);
    more.hidden = !next;
  });
  state.refresh = async () => {
    const fresh = await api('/api/activity?' + query + '&limit=30');
    const newest = events.length ? events[0].seq : 0;
    const added = fresh.events.filter((event) => event.seq > newest);
    if (!added.length) { refreshTimes(); return; }
    if (!events.length) { route(); return; }
    events.unshift(...added);
    const target = document.getElementById('events');
    target.insertAdjacentHTML('afterbegin', part(added.map((event) => eventRow(event))));
    bindEvents(target);
  };
};

function activityHead(group, run, memory) {
  if (run) return pageHead('One learning run', 'Every idea it considered, and what was tidied up.', { back: ['#/learning', 'Learning'] });
  if (memory) return pageHead('History of one memory', '', { back: [`#/memories/${memory}`, 'The memory'] });
  return html`
    ${pageHead('Activity', 'Everything KnowItAll2 did, newest first. It keeps 90 days.', { live: true })}
    <div class="toolbar"><div class="segmented" role="group" aria-label="Show">
      ${GROUPS.map(([key, label]) => html`<a href="#/activity?group=${key}" class="${key === group ? 'on' : ''}">${label}</a>`)}
    </div></div>`;
}

const eventCache = new Map();

function eventRow(event, options = {}) {
  eventCache.set(event.seq, event);
  const expandable = !options.compact;
  const open = state.expanded.has(event.seq);
  return html`
    <li class="event ${expandable ? 'expandable' : ''}" data-seq="${event.seq}">
      ${timeTag(event.at, 'time')}
      <div class="body">
        <div class="line"><span class="kind">${ACTIVITY_WORDS[event.kind] || event.kind}</span>
          <span class="summary">${event.summary}</span></div>
        <div class="meta">${outcomeChip(event.outcome)}
          ${event.kind === 'candidate' && event.details.reason ? html`<span class="chip bad">${reasonWord(event.details.reason)}</span>` : ''}
          ${event.agent ? html`<span class="chip">${event.agent}</span>` : ''}
          ${event.project_name ? html`<span class="chip teal">${event.project_name}</span>` : ''}</div>
        ${expandable && open ? eventDetails(event) : ''}
      </div>
    </li>`;
}

function bindEvents(list) {
  list.querySelectorAll('.event.expandable:not([data-bound])').forEach((row) => {
    row.dataset.bound = '1';
    row.addEventListener('click', (click) => {
      if (click.target.closest('a, button, .details')) return;
      const seq = Number(row.dataset.seq);
      const body = row.querySelector('.body');
      const existing = body.querySelector('.details');
      if (existing) {
        existing.remove();
        state.expanded.delete(seq);
        return;
      }
      state.expanded.add(seq);
      const event = eventCache.get(seq);
      if (event) body.insertAdjacentHTML('beforeend', part(eventDetails(event)));
    });
  });
}

function eventDetails(event) {
  const d = event.details || {};
  const rows = [];
  const add = (label, value) => { if (value !== undefined && value !== null && value !== '' && !(Array.isArray(value) && !value.length)) rows.push(html`<dt>${label}</dt><dd>${value}</dd>`); };
  add('When', fullTime(event.at));
  if (event.kind === 'learning') {
    add('What happened', runSentence(event));
    add('Sessions read', (d.sessions || []).length ? (d.sessions || []).map((item) => item.session.slice(0, 8)).join(', ') : 'none');
    add('Ideas', Object.entries(d.outcomes || {}).map(([name, count]) => `${outcomeWords(name)}: ${count}`).join(', '));
    add('Took', d.seconds !== undefined ? `${Math.round(d.seconds)} seconds` : '');
    add('Model calls', d.calls);
    if (d.maintenance && d.maintenance.calls) add('Tidy-up', `${plural(d.maintenance.calls, 'call')}${Object.keys(d.maintenance.changes || {}).length ? ': ' + Object.entries(d.maintenance.changes).map(([name, count]) => `${OUTCOME_WORDS[name] || name} ${count}`).join(', ') : ''}`);
    if (d.catalog && d.catalog.calls) add('Sorting', `${plural(d.catalog.filed || 0, 'thing')} sorted, ${plural(d.catalog.profiled || 0, 'system')} described`);
    if (event.run_id) add('Ideas', html`<a href="#/activity?run=${event.run_id}">See every idea in this run</a>`);
  } else if (event.kind === 'candidate') {
    add('Kind', kindWord(d.kind));
    add('Why', d.reason ? reasonWord(d.reason) : (OUTCOME_WORDS[d.result] || d.result));
    if (d.evidence) add('What the agent quoted', html`<span class="quote">${d.evidence}</span>`);
  } else if (event.kind === 'change') {
    add('Why', d.reason);
    add('Kept', d.kept);
  } else {
    Object.entries(d).forEach(([key, value]) => add(key, typeof value === 'object' ? JSON.stringify(value) : value));
  }
  if (event.record_ids && event.record_ids.length) {
    add('Memories', raw(event.record_ids.slice(0, 12).map((id) => part(html`<a class="mono" href="#/memories/${id}">${id}</a>`)).join(' ')));
  }
  return html`<dl class="kv details">${rows}</dl>`;
}

function problemRow(problem) {
  return html`
    <li class="event">
      ${timeTag(problem.at, 'time')}
      <div class="body"><div class="line"><span class="kind">${problem.source}</span><span class="summary">${problem.message}</span></div></div>
    </li>`;
}

/* ---------- Start ---------- */

if (!state.key) {
  showLocked();
} else {
  heartbeat();
  route();
}
