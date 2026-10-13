/* Team Builder front end. Plain JavaScript, no build step, no external resources.
 *
 * Everything that comes from the server or from a model is untrusted text. It is only ever put on
 * the page with textContent (through the h() helper below), never as markup.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------------------------------

  const VIEWS = ['guard', 'create', 'audit', 'run', 'settings'];
  const TOKEN_HEADER = 'X-Team-Build-Token';
  const DEFAULT_LEGS = {
    private_data: { letter: 'P', label: 'Private data', text: 'Can read files on your computer.' },
    untrusted_content: { letter: 'U', label: 'Untrusted content', text: 'Reads web pages or messages that someone else wrote.' },
    exfiltration: { letter: 'E', label: 'Can send out', text: 'Can send information off your computer.' },
    gated: { letter: 'G', label: 'Needs your approval', text: 'Stops and asks you before it acts.' },
  };
  const PROP_ATTRS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'open']);

  const state = {
    status: null,
    blueprints: null,
    legs: null,
    current: null,
    plannerOptionEl: null,
    prefill: null,
  };

  function tokenValue() {
    const m = document.querySelector('meta[name="tb-token"]');
    return m ? m.getAttribute('content') || '' : '';
  }
  const TOKEN = tokenValue();

  let uidCounter = 0;
  function uid(prefix) {
    uidCounter += 1;
    return (prefix || 'id') + '-' + uidCounter;
  }

  /** Build an element. attrs: class, on<event> functions, property-style booleans, plain attributes. */
  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const v = attrs[key];
        if (v === null || v === undefined || v === false) continue;
        if (key === 'class') el.className = v;
        else if (key === 'style') continue; // styling is done with classes only
        else if (key.startsWith('on')) {
          if (typeof v === 'function') el.addEventListener(key.slice(2), v);
        } else if (PROP_ATTRS.has(key)) el[key] = v;
        else el.setAttribute(key, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function append(parent, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) {
      child.forEach((c) => append(parent, c));
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }

  function clear(node) {
    node.innerHTML = '';
  }

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function safeUrl(u) {
    try {
      const url = new URL(u, window.location.origin);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
    } catch (e) {
      return null;
    }
  }

  function announce(msg) {
    const live = $('#live');
    if (!live) return;
    live.textContent = '';
    window.setTimeout(() => { live.textContent = msg; }, 30);
  }

  function store(key, value) {
    try {
      if (value === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* storage may be blocked; the in-memory copy is enough */ }
  }
  function load(key) {
    try {
      const raw = window.sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function fmtCost(n) {
    const v = Number(n) || 0;
    if (v > 0 && v < 0.01) return '<$0.01';
    return '$' + v.toFixed(2);
  }

  function legInfo(key) {
    return (state.legs && state.legs[key]) || DEFAULT_LEGS[key] || { letter: '?', label: String(key), text: '' };
  }

  // ------------------------------------------------------------------------------------------
  // Talking to the server
  // ------------------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.status = status;
      this.body = body;
    }
  }

  function detailText(data, status) {
    const d = data && data.detail;
    if (typeof d === 'string' && d) {
      return status === 403 ? d + ' Try reloading this page.' : d;
    }
    if (Array.isArray(d) && d.length) {
      return d.map((item) => {
        if (typeof item === 'string') return item;
        const where = Array.isArray(item.loc) ? item.loc.filter((x) => x !== 'body' && x !== 'query').join(' / ') : '';
        const msg = item.msg || 'is not valid';
        return where ? where + ': ' + msg : msg;
      }).join('; ');
    }
    if (d && typeof d === 'object') {
      try { return JSON.stringify(d); } catch (e) { /* fall through */ }
    }
    return 'Something went wrong (code ' + status + ').';
  }

  async function api(method, path, body) {
    const opts = { method: method, headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      opts.headers[TOKEN_HEADER] = TOKEN;
      opts.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError('Could not reach Team Builder. Is it still running on this computer?', 0, null);
    }
    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch (e) {
      data = null;
    }
    if (!res.ok) throw new ApiError(detailText(data, res.status), res.status, data);
    return data;
  }

  /** A visible inline error box. */
  function alertBox() {
    const el = h('div', { class: 'alert alert-error', role: 'alert', hidden: true });
    return {
      el: el,
      show(msg) { el.textContent = msg; el.hidden = false; },
      clear() { el.textContent = ''; el.hidden = true; },
    };
  }

  function notice(kind, ...children) {
    return h('div', { class: 'notice notice-' + kind }, children);
  }

  /** Disable buttons while an async job runs, then put them back as they were. */
  async function withBusy(buttons, fn) {
    const list = (Array.isArray(buttons) ? buttons : [buttons]).filter(Boolean);
    const before = list.map((b) => b.disabled);
    list.forEach((b) => { b.disabled = true; b.classList.add('is-busy'); });
    try {
      return await fn();
    } finally {
      list.forEach((b, i) => { b.disabled = before[i]; b.classList.remove('is-busy'); });
    }
  }

  // ------------------------------------------------------------------------------------------
  // Shared pieces: chips, verdicts, folder picker
  // ------------------------------------------------------------------------------------------

  function chip(key) {
    const info = legInfo(key);
    return h('span', { class: 'chip chip-' + info.letter },
      h('span', { class: 'chip-letter', 'aria-hidden': 'true' }, info.letter),
      h('span', null, info.label));
  }

  const VERDICT = {
    PASS: { icon: '✓', cls: 'ok' },
    WARN: { icon: '⚠', cls: 'warn' },
    FAIL: { icon: '✕', cls: 'bad' },
  };

  function verdictPill(verdict) {
    const v = VERDICT[verdict] || { icon: '–', cls: 'info' };
    return h('span', { class: 'pill pill-' + v.cls },
      h('span', { 'aria-hidden': 'true' }, v.icon), h('span', null, verdict));
  }

  /**
   * A home-scoped folder picker.
   * opts: {label, allowHome, onChoose(path)}; returns {el, ensureLoaded(), getChosen(), setChosen(path)}.
   */
  function folderPicker(opts) {
    const id = uid('picker');
    const err = alertBox();
    let cur = null;
    let chosen = null;
    let showHidden = false;
    let loaded = false;

    const whereEl = h('div', { class: 'picker-where', tabindex: '-1', 'aria-live': 'polite' });
    const upBtn = h('button', { type: 'button', class: 'btn btn-small', disabled: true, onclick: () => { if (cur && cur.parent) browse(cur.parent, true); } }, 'Up');
    const hiddenCb = h('input', { type: 'checkbox', id: id + '-hidden', onchange: () => { showHidden = hiddenCb.checked; browse(cur ? cur.path : '', false); } });
    const list = h('ul', { class: 'picker-list' });
    const chooseBtn = h('button', { type: 'button', class: 'btn btn-primary btn-small', disabled: true, onclick: () => { if (cur) choose(cur.path); } }, 'Choose this folder');
    const homeHint = h('p', { class: 'hint', hidden: true });
    const scopeHint = h('p', { class: 'hint', id: id + '-hint' });
    function rootLabel() {
      return (state.status && state.status.home_label) || 'your home folder';
    }
    const chosenEl = h('p', { class: 'chosen', 'aria-live': 'polite' });

    function renderChosen() {
      clear(chosenEl);
      if (chosen) append(chosenEl, ['Chosen folder: ', h('strong', { class: 'mono' }, chosen)]);
      else append(chosenEl, h('span', { class: 'muted' }, 'No folder chosen yet.'));
    }

    function render(userMoved) {
      const label = rootLabel();
      scopeHint.textContent = 'Only folders inside ' + label + ' are shown.';
      homeHint.textContent = 'Open a folder inside ' + label + ' first. ' + label + ' itself cannot be used here.';
      clear(whereEl);
      append(whereEl, ['Looking in: ', h('code', null, cur.path)]);
      if (cur.has_team) append(whereEl, [' ', h('span', { class: 'tag tag-info' }, 'has a team')]);
      upBtn.disabled = !cur.parent;
      const atHome = cur.path === cur.home;
      const blocked = !opts.allowHome && atHome;
      chooseBtn.disabled = blocked;
      homeHint.hidden = !blocked;
      clear(list);
      if (!cur.entries.length) {
        list.appendChild(h('li', { class: 'picker-empty' }, 'No sub-folders here.'));
      }
      cur.entries.forEach((e) => {
        list.appendChild(h('li', null,
          h('button', { type: 'button', class: 'picker-item', onclick: () => browse(e.path, true) },
            h('span', { class: 'picker-name' }, '📁 ' + e.name),
            e.has_team ? h('span', { class: 'tag tag-info' }, 'has a team') : null)));
      });
      if (cur.entries.length >= 500) {
        list.appendChild(h('li', { class: 'picker-empty' }, 'Only the first 500 folders are shown.'));
      }
      if (userMoved) whereEl.focus();
    }

    async function browse(path, userMoved) {
      err.clear();
      const q = new URLSearchParams();
      if (path) q.set('path', path);
      if (showHidden) q.set('hidden', 'true');
      const qs = q.toString();
      try {
        cur = await api('GET', '/api/folders' + (qs ? '?' + qs : ''));
        loaded = true;
        render(userMoved);
      } catch (e) {
        err.show(e.message);
        if (path && !cur) { // e.g. the saved folder is gone: fall back to the start folder
          try { cur = await api('GET', '/api/folders'); loaded = true; render(false); } catch (e2) { /* keep the error */ }
        }
      }
    }

    function choose(path, silent) {
      chosen = path;
      renderChosen();
      if (opts.onChoose && !silent) opts.onChoose(path);
    }

    renderChosen();
    scopeHint.textContent = 'Only folders inside ' + rootLabel() + ' are shown.';
    const el = h('fieldset', { class: 'picker', 'aria-describedby': id + '-hint' },
      h('legend', null, opts.label || 'Folder'),
      scopeHint,
      whereEl,
      h('div', { class: 'picker-tools' },
        upBtn,
        h('label', { class: 'choice', for: id + '-hidden' }, hiddenCb, h('span', { class: 'choice-text' }, 'Show hidden folders'))),
      list, homeHint,
      h('div', null, chooseBtn),
      chosenEl, err.el);

    return {
      el: el,
      ensureLoaded() { if (!loaded) browse('', false); },
      getChosen() { return chosen; },
      setChosen(path, silent) { choose(path, silent); browse(path, false); },
    };
  }

  // ------------------------------------------------------------------------------------------
  // Header, footer, routing
  // ------------------------------------------------------------------------------------------

  function renderBoard() {
    const slot = $('#board-slot');
    const hint = $('#board-hint');
    clear(slot);
    const board = state.status && state.status.board;
    const url = board ? safeUrl(board.url) : null;
    if (board && board.running && url) {
      slot.appendChild(h('a', { class: 'board-link', href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Board'));
      hint.hidden = true;
    } else {
      slot.appendChild(h('span', { class: 'board-off', 'aria-disabled': 'true' }, 'Board'));
      hint.hidden = false;
    }
  }

  async function refreshStatus() {
    try {
      state.status = await api('GET', '/api/status');
      $('#page-error').hidden = true;
    } catch (e) {
      const box = $('#page-error');
      box.textContent = e.message;
      box.hidden = false;
    }
    renderBoard();
    updatePlannerOption();
  }

  function updatePlannerOption() {
    if (!state.plannerOptionEl) return;
    const p = state.status && state.status.planner;
    state.plannerOptionEl.hidden = !(p && p.provider && p.provider !== 'none');
  }

  let firstRoute = true;
  function route() {
    let name = window.location.hash.replace(/^#/, '');
    if (VIEWS.indexOf(name) < 0) name = 'guard';
    const previous = state.current;
    state.current = name;
    document.querySelectorAll('[data-view]').forEach((sec) => { sec.hidden = sec.getAttribute('data-view') !== name; });
    document.querySelectorAll('[data-tab]').forEach((a) => {
      if (a.getAttribute('data-tab') === name) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    if (name === 'audit' && auditPicker) auditPicker.ensureLoaded();
    if (name === 'guard') enterGuard();
    if (name === 'settings' && settingsPicker) settingsPicker.ensureLoaded();
    if (name === 'run') enterRun();
    if (!firstRoute && previous !== name) {
      const heading = $('#view-' + name + ' h2');
      if (heading) heading.focus();
    }
    firstRoute = false;
  }

  // ------------------------------------------------------------------------------------------
  // GUARD
  // ------------------------------------------------------------------------------------------

  const G = { picker: null, statusEl: null, err: null, folder: null, sitesLoaded: false };

  const SCOPES = [
    ['local', 'This project, only me', 'Written to .claude/settings.local.json, which Claude Code keeps out of git.'],
    ['shared', 'This project, everyone who opens it', 'Written to .claude/settings.json: commit it so your team gets it too.'],
    ['user', 'Every project on this computer', 'Written to ~/.claude/settings.json. Remove it from here the same way.'],
  ];

  function buildGuard(root) {
    G.err = alertBox();
    G.statusEl = h('div', { class: 'guard-status', 'aria-live': 'polite' });
    G.picker = folderPicker({ label: 'Project folder', allowHome: false, onChoose: (p) => { G.folder = p; refreshGuard(); refreshApprovals(); } });
    const scopeName = uid('scope');
    const scopeInputs = SCOPES.map((s, i) => {
      const id = uid('scope');
      const input = h('input', { type: 'radio', name: scopeName, id: id, value: s[0], checked: i === 0 });
      return { input: input, el: h('label', { class: 'choice', for: id }, input,
        h('span', { class: 'choice-text' }, h('strong', null, s[1]), h('span', { class: 'hint' }, s[2]))) };
    });
    const mcpId = uid('mcp');
    const mcp = h('input', { type: 'checkbox', id: mcpId });
    const sensId = uid('sens');
    const sensitive = h('input', { type: 'text', id: sensId, class: 'input mono', placeholder: 'config/*, *.pem, customers/*', autocomplete: 'off', spellcheck: 'false' });
    const flowId = uid('flow');
    const flow = h('select', { id: flowId, class: 'input' },
      h('option', { value: 'block' }, 'refuse anything leaving this computer for the rest of the session'),
      h('option', { value: 'ask' }, 'still ask me on the board, with a red warning'));
    const installBtn = h('button', { type: 'button', class: 'btn btn-primary', onclick: onInstall }, 'Turn guard mode on');
    const removeBtn = h('button', { type: 'button', class: 'btn', onclick: () => onRemove('project') }, 'Turn off for this project');
    const removeUserBtn = h('button', { type: 'button', class: 'btn', onclick: () => onRemove('user') }, 'Turn off for every project');

    append(root, [
      h('p', null, 'With guard mode on, every web fetch and web search Claude Code makes, and every shell command that can reach the network (curl, ssh, git push, a web address...), waits for your Allow or Deny on the board. Calls that look like they carry data out get red warnings, and every decision is logged. It works in any Claude Code session; no team needed.'),
      h('div', { class: 'card' }, G.picker.el),
      h('div', { class: 'card' },
        h('fieldset', null, h('legend', null, 'Where'), scopeInputs.map((s) => s.el)),
        h('fieldset', null, h('legend', null, 'Options'),
          h('label', { class: 'choice', for: mcpId }, mcp, h('span', { class: 'choice-text' }, h('strong', null, 'Ask for MCP tools too'),
            h('span', { class: 'hint' }, 'The board cannot tell an MCP tool that reads from one that sends, so this asks for all of them.'))),
          h('label', { for: sensId }, 'Sensitive files (optional, comma-separated, relative to the project)'),
          sensitive,
          h('label', { for: flowId }, 'Once a session has read one of them'),
          flow),
        h('div', { class: 'row step-note' }, installBtn, removeBtn, removeUserBtn),
        G.err.el, G.statusEl),
      buildBoardCard(),
      buildSitesCard(),
      buildApprovalsCard(),
      h('div', { class: 'card' }, h('h3', null, 'Or install it as a Claude Code plugin'),
        h('p', null, 'The same guard for every project, managed from inside Claude Code. Type these in a Claude Code session:'),
        h('pre', null, h('code', null, '/plugin marketplace add Manuele-T/agent-troops\n/plugin install agent-troops@agent-troops')),
        h('p', { class: 'hint' }, 'Options (sensitive files, MCP tools) are under /config once it is installed. /plugin turns it off again.')),
      h('p', { class: 'hint' }, 'The list of commands that get a card is a filter, not a sandbox: a program can reach the network in ways it does not know. It is a second line after Claude Code\u2019s own permission prompts.'),
    ]);

    function chosenScope() {
      const s = scopeInputs.find((x) => x.input.checked);
      return s ? s.input.value : 'local';
    }

    async function onInstall() {
      G.err.clear();
      if (!G.folder) { G.err.show('Choose a project folder first.'); return; }
      const body = { folder: G.folder, scope: chosenScope(), mcp: mcp.checked, flow: flow.value,
        sensitive: sensitive.value.split(',').map((x) => x.trim()).filter(Boolean) };
      await withBusy([installBtn, removeBtn, removeUserBtn], async () => {
        try {
          renderGuardStatus(await api('POST', '/api/guard/install', body));
          announce('Guard mode is on.');
        } catch (e) { G.err.show(e.message); }
      });
    }

    async function onRemove(scope) {
      G.err.clear();
      if (!G.folder) { G.err.show('Choose a project folder first.'); return; }
      await withBusy([installBtn, removeBtn, removeUserBtn], async () => {
        try {
          renderGuardStatus(await api('POST', '/api/guard/remove', { folder: G.folder, scope: scope }));
          announce('Done.');
        } catch (e) { G.err.show(e.message); }
      });
    }
  }

  function renderGuardStatus(res) {
    clear(G.statusEl);
    const on = (res.installs || []).length || res.plugin;
    G.statusEl.appendChild(notice(on ? 'ok' : 'info',
      h('strong', null, on ? 'Guard mode is on' : 'Guard mode is off for this project'),
      h('pre', { class: 'status-text' }, res.text || ''),
      (res.actions && res.actions.length) ? h('ul', { class: 'hint' }, res.actions.map((a) => h('li', null, a))) : null,
      on ? h('p', { class: 'hint' }, 'It applies to Claude Code sessions started after the change.') : null));
  }

  async function refreshGuard() {
    if (!G.folder) return;
    G.err.clear();
    try {
      renderGuardStatus(await api('GET', '/api/guard?path=' + encodeURIComponent(G.folder)));
    } catch (e) { G.err.show(e.message); }
  }

  function buildBoardCard() {
    const body = h('div', null);
    G.boardEl = body;
    renderGuardBoard();
    return h('div', { class: 'card' }, h('h3', null, 'The board'), body);
  }

  function renderGuardBoard() {
    if (!G.boardEl) return;
    clear(G.boardEl);
    const board = state.status && state.status.board;
    const url = board ? safeUrl(board.url) : null;
    if (board && board.running && url) {
      append(G.boardEl, h('p', null, 'Running. Approvals appear there: ',
        h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, 'open the board'), '.'));
    } else {
      append(G.boardEl, [
        h('p', null, 'Not running. Without it, Claude Code asks in the terminal as usual. Start it in a terminal:'),
        h('pre', null, h('code', null, 'team-build board')),
        h('button', { type: 'button', class: 'btn btn-small', onclick: async () => { await refreshStatus(); renderGuardBoard(); } }, 'Check again'),
      ]);
    }
  }

  function buildSitesCard() {
    const err = alertBox();
    const ok = h('p', { class: 'hint', 'aria-live': 'polite' });
    const allowId = uid('allow');
    const blockId = uid('block');
    const allow = h('textarea', { id: allowId, class: 'input mono', rows: 5, spellcheck: 'false' });
    const block = h('textarea', { id: blockId, class: 'input mono', rows: 5, spellcheck: 'false' });
    const save = h('button', { type: 'button', class: 'btn', onclick: onSave }, 'Save site lists');
    G.loadSites = async () => {
      if (G.sitesLoaded) return;
      try {
        const res = await api('GET', '/api/sites');
        allow.value = (res.allow || []).join('\n');
        block.value = (res.block || []).join('\n');
        ok.textContent = 'Saved in ' + res.path;
        G.sitesLoaded = true;
      } catch (e) { err.show(e.message); }
    };
    async function onSave() {
      err.clear();
      const lines = (t) => t.value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
      await withBusy(save, async () => {
        try {
          const res = await api('POST', '/api/sites', { allow: lines(allow), block: lines(block) });
          allow.value = res.allow.join('\n');
          block.value = res.block.join('\n');
          ok.textContent = 'Saved in ' + res.path + '. The board uses them at once.';
        } catch (e) { err.show(e.message); }
      });
    }
    return h('div', { class: 'card' }, h('h3', null, 'Site lists'),
      h('p', null, 'One site per line. A name covers its subdomains. Allowed sites run without a card (unless a call has warnings); blocked sites are refused without asking.'),
      h('div', { class: 'grid-2' },
        h('div', null, h('label', { for: allowId }, 'Allowed sites'), allow),
        h('div', null, h('label', { for: blockId }, 'Blocked sites'), block)),
      h('div', { class: 'row step-note' }, save), err.el, ok);
  }

  function buildApprovalsCard() {
    const err = alertBox();
    const out = h('div', { 'aria-live': 'polite' });
    const btn = h('button', { type: 'button', class: 'btn btn-small', onclick: () => refreshApprovals() }, 'Refresh');
    G.approvalsOut = out;
    G.approvalsErr = err;
    return h('div', { class: 'card' }, h('h3', null, 'Recent decisions'),
      h('p', { class: 'hint' }, 'From the approval log: the chosen project only, or every project when none is chosen. Secrets are redacted before they are written.'),
      h('div', { class: 'row' }, btn), err.el, out);
  }

  async function refreshApprovals() {
    if (!G.approvalsOut) return;
    G.approvalsErr.clear();
    let res;
    try {
      res = await api('GET', '/api/approvals?limit=30' + (G.folder ? '&path=' + encodeURIComponent(G.folder) : ''));
    } catch (e) { G.approvalsErr.show(e.message); return; }
    clear(G.approvalsOut);
    const entries = res.entries || [];
    if (!entries.length) {
      G.approvalsOut.appendChild(h('p', { class: 'muted' }, 'No decisions recorded yet.'));
      return;
    }
    const table = h('table', { class: 'audit' }, h('thead', null, h('tr', null,
      ['When', 'Decision', 'Who', 'Call', 'Why'].map((t) => h('th', { scope: 'col' }, t)))));
    const tbody = h('tbody', null);
    entries.forEach((e) => {
      const when = typeof e.ts === 'number' ? new Date(e.ts * 1000).toLocaleString() : '?';
      const allowed = e.decision === 'allow';
      tbody.appendChild(h('tr', null,
        h('td', { 'data-label': 'When' }, when),
        h('td', { 'data-label': 'Decision' }, h('span', { class: 'tag tag-' + (allowed ? 'ok' : 'bad') }, allowed ? 'allowed' : 'denied')),
        h('td', { 'data-label': 'Who' }, String(e.agent || '')),
        h('td', { 'data-label': 'Call' }, h('div', null, String(e.tool || '')), h('div', { class: 'hint mono' }, String(e.detail || '').slice(0, 200))),
        h('td', { 'data-label': 'Why' }, String(e.how || ''),
          (e.warnings && e.warnings.length) ? h('ul', { class: 'hint' }, e.warnings.map((w) => h('li', null, String(w)))) : null)));
    });
    table.appendChild(tbody);
    G.approvalsOut.appendChild(h('div', { class: 'table-wrap' }, table));
  }

  function enterGuard() {
    if (G.picker) G.picker.ensureLoaded();
    if (G.loadSites) G.loadSites();
    renderGuardBoard();
    refreshApprovals();
  }

  // ------------------------------------------------------------------------------------------
  // CREATE
  // ------------------------------------------------------------------------------------------

  let createPicker = null;

  function isNoneChip(text) {
    return /^\s*(nothing|skip)/i.test(text || '');
  }

  const PREFIX_RE = /^[a-z][a-z0-9-]{0,15}$/;

  function buildCreate(root, data) {
    const bps = data.blueprints || [];
    const C = { bp: null, preview: null, snap: null, prefixTouched: false, created: null };

    const briefEl = h('textarea', { id: 'brief', rows: 3, maxlength: 4000, 'data-affects': '1', placeholder: 'For example: research what my competitors charge and write me a short report' });
    const suggestErr = alertBox();
    const suggestMsg = h('p', { class: 'muted', 'aria-live': 'polite' });
    const suggestBtn = h('button', { type: 'button', class: 'btn', onclick: onSuggest }, 'Suggest a team');

    // ---- team cards
    const cardEls = {};
    const cards = h('div', { class: 'team-cards' });
    bps.forEach((bp) => {
      const input = h('input', { type: 'radio', name: 'team', value: bp.id, class: 'card-input', onchange: () => selectBlueprint(bp.id) });
      const label = h('label', { class: 'team-card', 'data-bp': bp.id },
        input,
        h('span', { class: 'card-body' },
          h('span', { class: 'card-title' }, bp.title),
          h('span', { class: 'card-sub' }, bp.roles.length + ' roles'),
          h('span', { class: 'card-roles' }, bp.roles.map((r) => r.display_name).join(', ')),
          h('span', { class: 'tag tag-warn suggest-tag' }, 'Suggested'),
          h('span', { class: 'card-check' }, '✓ Selected')));
      cardEls[bp.id] = { label: label, input: input };
      cards.appendChild(label);
    });

    // ---- steps 2-4
    const questionsBox = h('div');
    const guardrailBox = h('div');
    const prefixInput = h('input', {
      type: 'text', id: 'prefix', maxlength: 16, pattern: '[a-z][a-z0-9\\-]{0,15}', autocomplete: 'off',
      spellcheck: 'false', 'data-affects': '1', 'aria-describedby': 'prefix-hint prefix-err',
      oninput: () => { C.prefixTouched = true; validatePrefix(false); },
    });
    const prefixErr = h('p', { class: 'field-error', id: 'prefix-err', hidden: true });
    createPicker = folderPicker({
      label: 'Project folder',
      allowHome: false,
      onChoose: () => { updateCreateEnabled(); },
    });
    const fenceCb = h('input', { type: 'checkbox', id: 'fence', 'data-affects': '1' });
    const flowAskCb = h('input', { type: 'checkbox', id: 'flow-ask', 'data-affects': '1' });
    const flowRow = h('div', { class: 'choice', hidden: true }, flowAskCb,
      h('label', { class: 'choice-text', for: 'flow-ask' }, 'Ask me instead of refusing when the team wants more web research after reading files',
        h('small', null, 'This team reads the web and reads files, so it follows a one-way rule: once files have been read, no more web in that session. Normally that is simply refused; tick this to be asked instead.')));
    const plannerCb = h('input', { type: 'checkbox', id: 'use-planner', 'data-affects': '1' });
    const plannerRow = h('div', { class: 'choice' }, plannerCb,
      h('label', { class: 'choice-text', for: 'use-planner' }, 'Let my planner tailor the roles (slower, optional)',
        h('small', null, 'The built-in templates are used if the planner misbehaves.')));
    state.plannerOptionEl = plannerRow;
    updatePlannerOption();

    const previewErr = alertBox();
    const previewBtn = h('button', { type: 'button', class: 'btn btn-primary', onclick: onPreview }, 'Preview team');
    const previewOut = h('div');
    const overwriteOut = h('div');
    const createErr = alertBox();
    const resultOut = h('div');

    const steps = h('div', { hidden: true },
      h('section', { class: 'step' },
        h('h3', null, '2. A few questions'),
        questionsBox, guardrailBox),
      h('section', { class: 'step' },
        h('h3', null, '3. Options'),
        h('div', { class: 'field' },
          h('label', { class: 'field-label', for: 'prefix' }, 'Team name'),
          prefixInput,
          h('p', { class: 'hint', id: 'prefix-hint' }, 'Put at the start of each team member\'s name, like web-builder. Lowercase letters, numbers and hyphens. Start with a letter. Up to 16 characters.'),
          prefixErr),
        createPicker.el,
        h('div', { class: 'choice' }, fenceCb,
          h('label', { class: 'choice-text', for: 'fence' }, 'Keep team members inside this folder (blocks reading files elsewhere in every session here)')),
        flowRow,
        plannerRow),
      h('section', { class: 'step' },
        h('h3', null, '4. Preview your team'),
        h('p', null, 'See exactly what each team member can and cannot do before anything is written.'),
        h('div', null, previewBtn),
        previewErr.el,
        previewOut, overwriteOut, createErr.el));

    append(root, [
      h('p', null, 'A team is a small group of helpers, each allowed to do only the things its job needs. Pick one, look it over, then create it in a project folder.'),
      h('section', { class: 'step' },
        h('h3', null, '1. What should your team do?'),
        h('label', { class: 'field-label', for: 'brief' }, 'Describe the job in a sentence or two (optional if you already know which team you want)'),
        briefEl,
        h('div', { class: 'row step-note' }, suggestBtn),
        suggestErr.el, suggestMsg,
        h('fieldset', { class: 'step-note' }, h('legend', null, 'Pick a team'), cards)),
      steps,
      resultOut,
    ]);

    // Any change to an answer or option makes the old preview stale.
    root.addEventListener('input', onAffects);
    root.addEventListener('change', onAffects);
    function onAffects(ev) {
      const t = ev.target;
      if (t && t.hasAttribute && t.hasAttribute('data-affects')) clearPreview();
    }

    function clearPreview() {
      C.preview = null;
      C.snap = null;
      clear(previewOut);
      clear(overwriteOut);
      previewErr.clear();
      createErr.clear();
    }

    function validatePrefix(loud) {
      const v = prefixInput.value.trim();
      const ok = PREFIX_RE.test(v);
      if (ok) {
        prefixErr.hidden = true;
        prefixInput.removeAttribute('aria-invalid');
      } else if (loud || v) {
        prefixErr.textContent = 'Use 1 to 16 lowercase letters, numbers or hyphens, starting with a letter.';
        prefixErr.hidden = false;
        prefixInput.setAttribute('aria-invalid', 'true');
      }
      return ok;
    }

    function markSuggested(ids) {
      Object.keys(cardEls).forEach((k) => cardEls[k].label.classList.toggle('suggested', ids.indexOf(k) >= 0));
    }

    async function onSuggest() {
      suggestErr.clear();
      suggestMsg.textContent = '';
      const brief = briefEl.value.trim();
      if (!brief) { suggestErr.show('Describe the job first, or pick a team below.'); return; }
      await withBusy(suggestBtn, async () => {
        try {
          const res = await api('POST', '/api/preview', { brief: brief });
          if (res.needs_choice) {
            const ids = Array.isArray(res.candidates) ? res.candidates.filter((c) => cardEls[c]) : [];
            markSuggested(ids);
            suggestMsg.textContent = ids.length && ids.length < bps.length
              ? 'That fits more than one team, pick one.'
              : 'We could not tell from that. Pick the team that is closest.';
          } else if (res.blueprint_id && cardEls[res.blueprint_id]) {
            markSuggested([]);
            const bp = bps.find((b) => b.id === res.blueprint_id);
            cardEls[res.blueprint_id].input.checked = true;
            selectBlueprint(res.blueprint_id);
            suggestMsg.textContent = 'We picked "' + (bp ? bp.title : res.blueprint_id) + '" for you. Change it below if that is not right.';
          }
          announce(suggestMsg.textContent);
        } catch (e) {
          suggestErr.show(e.message);
        }
      });
    }

    function radioGroup(name, legend, options, checkedValue, affects) {
      const fs = h('fieldset', null, h('legend', null, legend));
      options.forEach((opt) => {
        const rid = uid('opt');
        const input = h('input', { type: 'radio', name: name, id: rid, value: opt, checked: opt === checkedValue, 'data-affects': affects ? '1' : null });
        fs.appendChild(h('div', { class: 'choice' }, input, h('label', { class: 'choice-text', for: rid }, opt)));
      });
      return fs;
    }

    function selectBlueprint(id) {
      const bp = bps.find((b) => b.id === id);
      if (!bp) return;
      C.bp = bp;
      clearPreview();
      clear(questionsBox);
      (bp.questions || []).forEach((q) => {
        questionsBox.appendChild(radioGroup('q-' + q.id, q.text, q.options || [], (q.options || [])[0], true));
      });
      clear(guardrailBox);
      const chips = bp.problem_chips || [];
      const none = chips.find(isNoneChip);
      guardrailBox.appendChild(radioGroup('guardrail', 'Anything that usually goes wrong that the team should watch for?', chips, none !== undefined ? none : null, true));
      if (!C.prefixTouched) prefixInput.value = bp.id.split('_')[0].slice(0, 4);
      const reachesWeb = (bp.roles || []).some((r) => !r.gated && (r.legs || []).indexOf('untrusted_content') >= 0 && (r.legs || []).indexOf('exfiltration') >= 0);
      const readsFiles = (bp.roles || []).some((r) => (r.legs || []).indexOf('private_data') >= 0);
      flowRow.hidden = !(reachesWeb && readsFiles);
      if (flowRow.hidden) flowAskCb.checked = false;
      validatePrefix(false);
      steps.hidden = false;
      createPicker.ensureLoaded();
    }

    function collect() {
      const answers = {};
      (C.bp.questions || []).forEach((q) => {
        const sel = root.querySelector('input[name="q-' + q.id + '"]:checked');
        if (sel) answers[q.id] = sel.value;
      });
      const g = root.querySelector('input[name="guardrail"]:checked');
      const guardrail = g && !isNoneChip(g.value) ? g.value : null;
      const target = createPicker.getChosen();
      const body = {
        brief: briefEl.value.trim(),
        blueprint_id: C.bp.id,
        answers: answers,
        guardrail: guardrail,
        prefix: prefixInput.value.trim(),
        fence_reads: fenceCb.checked,
        flow_mode: !flowRow.hidden && flowAskCb.checked ? 'ask' : 'block',
        use_planner: !plannerRow.hidden && plannerCb.checked,
      };
      if (target) body.target = target;
      return body;
    }

    async function onPreview() {
      previewErr.clear();
      if (!C.bp) { previewErr.show('Pick a team first.'); return; }
      if (!validatePrefix(true)) { prefixInput.focus(); return; }
      const body = collect();
      await withBusy(previewBtn, async () => {
        try {
          const res = await api('POST', '/api/preview', body);
          if (res.needs_choice) { previewErr.show('Pick a team above first.'); return; }
          clear(resultOut);
          C.preview = res;
          C.snap = { fence_reads: body.fence_reads };
          renderPreview();
          announce('Preview ready.');
        } catch (e) {
          previewErr.show(e.message);
        }
      });
    }

    let createBtn = null;
    function updateCreateEnabled() {
      if (!createBtn) return;
      const has = !!createPicker.getChosen();
      createBtn.disabled = !has;
      const msg = $('#create-need-folder');
      if (msg) msg.hidden = has;
    }

    function roleCard(role) {
      const legKeys = (role.legs || []).slice();
      if (role.gated && legKeys.indexOf('gated') < 0) legKeys.push('gated');
      const tools = role.tools || [];
      return h('article', { class: 'role-card' },
        h('div', { class: 'role-head' },
          h('h4', null, role.display_name || role.id),
          h('code', null, role.id),
          role.model ? h('span', { class: 'tag' }, 'model: ' + role.model) : null,
          role.guarded ? h('span', { class: 'tag tag-info' }, 'Limited shell') : null),
        role.description ? h('p', null, role.description) : null,
        legKeys.length
          ? h('ul', { class: 'legs', 'aria-label': 'What this team member can do' },
            legKeys.map((k) => h('li', null, chip(k), h('span', { class: 'leg-text' }, legInfo(k).text))))
          : h('p', { class: 'hint' }, 'This team member cannot read your files, read untrusted content or send anything out.'),
        h('div', null, h('span', { class: 'hint' }, 'Tools: '),
          tools.length
            ? h('ul', { class: 'tool-list' }, tools.map((t) => h('li', null, t)))
            : h('span', { class: 'muted' }, 'none, it can only talk')),
        (role.boundaries && role.boundaries.length)
          ? h('details', null, h('summary', null, 'What it can\'t do'),
            h('ul', null, role.boundaries.map((b) => h('li', null, b))))
          : null);
    }

    function renderPreview() {
      const p = C.preview;
      clear(previewOut);
      clear(overwriteOut);
      createBtn = null;
      const errors = Array.isArray(p.errors) ? p.errors : [];
      if (errors.length) {
        previewOut.appendChild(h('div', { class: 'notice notice-bad' },
          h('strong', null, 'This team cannot be created:'),
          h('ul', null, errors.map((e) => h('li', null, e)))));
      }
      if (p.note) previewOut.appendChild(notice('info', p.note));
      previewOut.appendChild(h('p', { class: 'hint' },
        p.source === 'planner' ? 'The roles were tailored by your planner.' : 'The roles come from the built-in templates.'));
      const roles = p.roles || [];
      const anyTrifecta = roles.some((r) => r.trifecta);
      if (!anyTrifecta) {
        previewOut.appendChild(notice('ok', 'No single team member can read your files, read the web, and send data out all at once.'));
        if (p.flow) {
          previewOut.appendChild(notice('ok', 'One-way flow: once ' + p.flow.private.join(', ') + ' has been used, '
            + p.flow.outbound.join(', ') + (p.flow.mode === 'ask' ? ' needs your OK' : ' gets no more work')
            + ' in that session, so private data can\'t be carried back out to the web.'));
        }
      } else {
        previewOut.appendChild(notice('warn', 'At least one team member can read your files, read untrusted content and send data out. Do not use this team.'));
      }
      if (p.guardrail) previewOut.appendChild(h('p', null, h('strong', null, 'Team guardrail: '), p.guardrail));
      roles.forEach((r) => previewOut.appendChild(roleCard(r)));
      const files = p.files || [];
      previewOut.appendChild(h('details', null, h('summary', null, 'Files that will be written (' + files.length + ')'),
        h('ul', { class: 'file-list' }, files.map((f) => h('li', null, f)))));

      if (!errors.length) {
        createBtn = h('button', { type: 'button', class: 'btn btn-primary', 'aria-describedby': 'create-need-folder', onclick: () => doCreate(false) }, 'Create team');
        previewOut.appendChild(h('div', { class: 'step-note' },
          h('h3', null, '5. Create it'),
          h('div', null, createBtn),
          h('p', { class: 'hint', id: 'create-need-folder', hidden: true }, 'Choose a project folder first (in step 3).')));
        updateCreateEnabled();
      }
    }

    async function doCreate(overwrite, extraBtn) {
      if (!C.preview) return;
      const target = createPicker.getChosen();
      createErr.clear();
      if (!target) { createErr.show('Choose a project folder first.'); return; }
      clear(overwriteOut);
      await withBusy([createBtn, extraBtn], async () => {
        try {
          const res = await api('POST', '/api/create', {
            roster: C.preview.roster, target: target, fence_reads: !!(C.snap && C.snap.fence_reads), overwrite: overwrite,
          });
          showCreated(res);
        } catch (e) {
          if (e.status === 409 && e.body && Array.isArray(e.body.exists)) showOverwrite(e.body.exists);
          else createErr.show(e.message);
        }
      });
    }

    function showOverwrite(existing) {
      clear(overwriteOut);
      const replaceBtn = h('button', { type: 'button', class: 'btn btn-danger', onclick: () => doCreate(true, replaceBtn) }, 'Replace');
      const cancelBtn = h('button', { type: 'button', class: 'btn', onclick: () => { clear(overwriteOut); } }, 'Cancel');
      overwriteOut.appendChild(h('div', { class: 'notice notice-warn', role: 'alert' },
        h('p', null, 'These files already exist here: ' + existing.join(', ') + '. Replace them?'),
        h('div', { class: 'row' }, replaceBtn, cancelBtn)));
    }

    function showCreated(res) {
      clear(overwriteOut);
      clear(previewOut);
      const p = C.preview;
      const orchestrator = res.orchestrator || p.orchestrator;
      C.created = { folder: res.folder, orchestrator: orchestrator, starter_task: p.starter_task || '' };
      const audit = res.audit || {};
      const cmdBox = h('code', { class: 'cmd-box', tabindex: '0' }, res.run_command || ('claude --agent ' + orchestrator));
      const copyMsg = h('span', { class: 'hint', 'aria-live': 'polite' });
      const copyBtn = h('button', { type: 'button', class: 'btn btn-small', onclick: () => copyText(cmdBox, copyMsg) }, 'Copy');
      const removeOut = h('div');
      const removeBtn = h('button', { type: 'button', class: 'btn-link', onclick: () => confirmRemove(removeOut, removeBtn, orchestrator, res.folder) }, 'Remove this team');
      clear(resultOut);
      resultOut.appendChild(h('section', { class: 'card', 'aria-labelledby': 'created-h' },
        h('h3', { id: 'created-h', tabindex: '-1' }, 'Team created'),
        h('p', null, 'Team created in ', h('code', null, res.folder)),
        h('p', null, h('strong', null, 'Safety check: '), verdictPill(audit.overall || 'PASS'),
          audit.overall === 'FAIL' ? ' Something in this folder breaks the safety rule. Run the Audit tab for details.' : ''),
        h('p', null, 'Prefer the terminal? Run this in the project folder:'),
        h('div', { class: 'stack' }, cmdBox, h('div', { class: 'row' }, copyBtn, copyMsg)),
        h('div', { class: 'row step-note' },
          h('button', { type: 'button', class: 'btn btn-primary', onclick: () => runItNow() }, 'Run it now'),
          removeBtn),
        removeOut));
      const head = $('#created-h');
      if (head) head.focus();
      announce('Team created.');
    }

    function runItNow() {
      if (!C.created) return;
      state.prefill = { folder: C.created.folder, team: C.created.orchestrator, job: C.created.starter_task };
      store('tb-prefill', state.prefill);
      window.location.hash = '#run';
    }

    function confirmRemove(out, trigger, orchestrator, folder) {
      const prefix = orchestrator.replace(/-orchestrator$/, '');
      clear(out);
      const err = alertBox();
      const yes = h('button', { type: 'button', class: 'btn btn-danger btn-small' }, 'Yes, remove it');
      const no = h('button', { type: 'button', class: 'btn btn-small', onclick: () => { clear(out); trigger.focus(); } }, 'Keep it');
      yes.addEventListener('click', () => withBusy([yes, no], async () => {
        try {
          const res = await api('POST', '/api/destroy', { prefix: prefix, target: folder, dry_run: false });
          C.created = null;
          clear(resultOut);
          resultOut.appendChild(notice('ok', 'The team "' + prefix + '" was removed (' + ((res.actions || []).length) + ' changes).'));
          announce('Team removed.');
        } catch (e) {
          err.show(e.message);
        }
      }));
      out.appendChild(h('div', { class: 'notice notice-warn' },
        h('p', null, 'Remove the team "' + prefix + '" from ' + folder + '? This deletes its files.'),
        h('div', { class: 'row' }, yes, no), err.el));
      yes.focus();
    }
  }

  function copyText(codeEl, msgEl) {
    const text = codeEl.textContent || '';
    const fallback = () => {
      try {
        const range = document.createRange();
        range.selectNodeContents(codeEl);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) { /* nothing more to try */ }
      msgEl.textContent = 'Copying did not work here. The line is selected: press Ctrl+C (or Cmd+C).';
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => { msgEl.textContent = 'Copied.'; },
        fallback);
    } else {
      fallback();
    }
  }

  // ------------------------------------------------------------------------------------------
  // AUDIT
  // ------------------------------------------------------------------------------------------

  let auditPicker = null;

  const AUDIT_TEXT = {
    PASS: 'No single agent can read your files, read untrusted content and send data out all at once.',
    WARN: 'Nothing breaks the main rule, but some things deserve a look.',
    FAIL: 'At least one agent can do too much. Read the notes below.',
    'NO AGENTS': 'No agents were found in this folder, so there is nothing to audit.',
  };

  function buildAudit(root) {
    const err = alertBox();
    const out = h('div', { 'aria-live': 'polite' });
    const btn = h('button', { type: 'button', class: 'btn btn-primary', onclick: onAudit }, 'Audit this folder');
    auditPicker = folderPicker({ label: 'Folder to audit', allowHome: true });
    append(root, [
      h('p', null, 'It checks whether any single agent can read your files, read untrusted content and send data out all at once.'),
      h('div', { class: 'card' }, auditPicker.el,
        h('div', { class: 'row step-note' }, btn)),
      err.el, out,
    ]);

    async function onAudit() {
      err.clear();
      const target = auditPicker.getChosen();
      if (!target) { err.show('Choose a folder first.'); return; }
      clear(out);
      await withBusy(btn, async () => {
        try {
          const res = await api('POST', '/api/audit', { target: target });
          renderAudit(out, res);
          announce('Audit finished: ' + res.overall);
        } catch (e) {
          err.show(e.message);
        }
      });
    }
  }

  function renderAudit(out, res) {
    clear(out);
    const overall = res.overall || 'NO AGENTS';
    const v = VERDICT[overall];
    out.appendChild(h('div', { class: 'verdict verdict-' + (v ? overall : 'NONE'), role: 'group', 'aria-label': 'Audit result' },
      h('span', { class: 'verdict-icon', 'aria-hidden': 'true' }, v ? v.icon : '–'),
      h('div', null, h('span', { class: 'verdict-word' }, overall), h('span', null, AUDIT_TEXT[overall] || ''))));

    const roles = res.roles || [];
    if (roles.length) {
      const table = h('table', { class: 'audit' },
        h('thead', null, h('tr', null,
          ['Role', 'Tools', 'Can do', 'Flags', 'Verdict'].map((t) => h('th', { scope: 'col' }, t)))));
      roles.forEach((r) => {
        const legKeys = (r.legs || []).slice();
        if (r.gated && legKeys.indexOf('gated') < 0) legKeys.push('gated');
        const toolsCell = r.tools === null || r.tools === undefined
          ? h('span', { class: 'tag tag-bad' }, 'Everything (no limit)')
          : (r.tools.length
            ? h('ul', { class: 'tool-list' }, r.tools.map((t) => h('li', null, t)))
            : h('span', { class: 'muted' }, 'none'));
        table.appendChild(h('tbody', { class: 'audit-item' },
          h('tr', { class: 'audit-main' },
            h('td', { 'data-label': 'Role' }, h('strong', null, r.id), r.path ? h('div', { class: 'hint mono' }, r.path) : null),
            h('td', { 'data-label': 'Tools' }, toolsCell),
            h('td', { 'data-label': 'Can do' }, legKeys.length ? h('div', { class: 'chip-row' }, legKeys.map(chip)) : h('span', { class: 'muted' }, 'nothing risky')),
            h('td', { 'data-label': 'Flags' }, h('div', { class: 'audit-flags' },
              r.gated ? h('span', { class: 'tag tag-ok' }, 'Asks you first') : null,
              r.guarded ? h('span', { class: 'tag tag-info' }, 'Limited shell') : null,
              !r.gated && !r.guarded ? h('span', { class: 'muted' }, 'none') : null)),
            h('td', { 'data-label': 'Verdict' }, verdictPill(r.verdict))),
          (r.notes && r.notes.length)
            ? h('tr', { class: 'notes-row' }, h('td', { colspan: 5 }, h('ul', null, r.notes.map((n) => h('li', null, n)))))
            : null));
      });
      out.appendChild(h('div', { class: 'table-wrap' }, table));
    }

    const notes = res.repo_notes || [];
    if (notes.length) {
      out.appendChild(h('section', { class: 'card' }, h('h3', null, 'Notes about this folder'),
        h('ul', null, notes.map((n) => {
          const lvl = String(n.level || 'INFO').toUpperCase();
          const cls = lvl === 'FAIL' ? 'bad' : lvl === 'WARN' ? 'warn' : 'info';
          return h('li', null, h('span', { class: 'tag tag-' + cls }, lvl), ' ', n.text);
        }))));
    }
    const skipped = res.skipped || [];
    if (skipped.length) {
      out.appendChild(h('section', { class: 'card' }, h('h3', null, 'Files that were skipped'),
        h('ul', null, skipped.map((s) => h('li', null, s)))));
    }
    if (typeof res.report === 'string' && res.report) {
      out.appendChild(h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn', onclick: () => downloadReport(res.report) }, 'Download report')));
    }
    if (res.roles && res.roles.length) {
      out.appendChild(h('p', { class: 'hint' },
        'This reads the agents’ settings. It cannot see what they actually do while running, and a pass does not mean the whole team is safe. The downloaded report lists the limits.'));
    }
  }

  function downloadReport(markdown) {
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'agent-audit.md';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ------------------------------------------------------------------------------------------
  // RUN: start a team in a terminal, already working on the job; follow it on the board
  // ------------------------------------------------------------------------------------------

  const R = {
    built: false,
    els: {},
    teamPicker: null,
    teams: [],
    mode: null,
  };

  function buildRun(root) {
    const e = R.els;
    e.noClaude = h('div', { class: 'card', hidden: true });

    const folderErr = alertBox();
    e.teamsBox = h('div', { 'aria-live': 'polite' });
    R.teamPicker = folderPicker({
      label: 'Team folder',
      allowHome: false,
      onChoose: (p) => loadTeams(p, null),
    });

    e.termJob = h('textarea', { id: 'term-job', rows: 4, maxlength: 20000, oninput: updateTerminalCommand,
      placeholder: 'For example: find the current prices of the top three note-taking apps and give me a short table' });
    e.termBtn = h('button', { type: 'button', class: 'btn btn-primary', onclick: onOpenTerminal }, 'Start in a terminal');
    e.termAuto = h('div', { class: 'row' }, e.termBtn);
    e.termNoAuto = h('p', { class: 'hint', hidden: true },
      'Opening a terminal for you only works on Linux for now. Copy the command below into your terminal.');
    e.termCmd = h('code', { class: 'cmd-box', tabindex: '0' }, 'Choose a folder and a team first.');
    e.termCopyMsg = h('span', { class: 'hint', 'aria-live': 'polite' });
    e.termCopyBtn = h('button', { type: 'button', class: 'btn btn-small', disabled: true,
      onclick: () => copyText(e.termCmd, e.termCopyMsg) }, 'Copy');
    e.termMsg = h('div', { 'aria-live': 'polite' });
    e.termErr = alertBox();
    e.termBoard = h('p');
    e.termCard = h('section', { class: 'card' },
      h('h3', null, 'Run it in a terminal'),
      h('p', null, 'Type the job here and press Start: Claude Code opens with the team already working on it. Then follow it on the board: approve its web and publishing steps there, and read each member\'s report and the final answer.'),
      h('label', { class: 'field-label', for: 'term-job' }, 'What should the team do?'),
      e.termJob,
      e.termAuto, e.termNoAuto,
      h('p', { class: 'step-note' }, 'Or run it yourself, in a terminal (Linux, macOS, WSL):'),
      e.termCmd,
      h('div', { class: 'row' }, e.termCopyBtn, e.termCopyMsg),
      e.termMsg, e.termErr.el, e.termBoard);

    e.form = h('div', null,
      h('p', null, 'Pick the folder your team lives in. The team runs in the Claude Code on this computer, with your own login, so it counts against your own plan.'),
      h('div', { class: 'card' }, R.teamPicker.el, folderErr.el, e.teamsBox),
      e.termCard);
    R.folderErr = folderErr;

    append(root, [e.noClaude, e.form]);
    R.built = true;
  }

  function claudeFound() {
    return !!(state.status && state.status.claude && state.status.claude.found);
  }

  async function enterRun() {
    if (!R.built) return;
    await refreshStatus();
    const e = R.els;
    if (!claudeFound()) {
      clear(e.noClaude);
      const url = safeUrl((state.status && state.status.install_url) || '');
      e.noClaude.appendChild(h('p', null, 'Running a team needs Claude Code installed and signed in on this computer. It was not found.'));
      if (url) e.noClaude.appendChild(h('p', null, h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, 'How to install it')));
      e.noClaude.hidden = false;
      e.form.hidden = true;
      return;
    }
    e.noClaude.hidden = true;
    e.form.hidden = false;
    await loadRunMode();
    R.teamPicker.ensureLoaded();
    applyPrefill();
  }

  async function loadRunMode() {
    try {
      R.mode = await api('GET', '/api/run-mode');
    } catch (err) {
      R.mode = null;
    }
    const e = R.els;
    const term = R.mode && R.mode.terminal;
    e.termAuto.hidden = !(term && term.auto_open);
    e.termNoAuto.hidden = !!(term && term.auto_open);
    clear(e.termBoard);
    const board = state.status && state.status.board;
    const bu = board && board.running ? safeUrl(board.url) : null;
    if (bu) e.termBoard.appendChild(h('a', { href: bu, target: '_blank', rel: 'noopener noreferrer' }, 'Open the board'));
    else e.termBoard.appendChild(h('span', { class: 'hint' }, 'The board is not running, so Claude Code will ask in the terminal instead. Start it with: team-build board'));
  }

  function shellQuote(text) {
    return /^[\w\/.,:@%+=-]+$/.test(text) ? text : "'" + text.replace(/'/g, "'\\''") + "'";
  }

  function selectedTeam() {
    const sel = R.els.teamsBox.querySelector('input[name="run-team"]:checked');
    return sel ? sel.value : null;
  }

  function updateTerminalCommand() {
    const e = R.els;
    const folder = R.teamPicker.getChosen();
    const team = selectedTeam();
    e.termCopyMsg.textContent = '';
    const job = e.termJob.value.trim();
    if (folder && team) {
      e.termCmd.textContent = 'cd ' + shellQuote(folder) + ' && claude --agent ' + team + (job ? ' ' + shellQuote(job) : '');
      e.termCopyBtn.disabled = false;
    } else {
      e.termCmd.textContent = 'Choose a folder and a team first.';
      e.termCopyBtn.disabled = true;
    }
  }

  async function onOpenTerminal() {
    const e = R.els;
    e.termErr.clear();
    clear(e.termMsg);
    const folder = R.teamPicker.getChosen();
    const team = selectedTeam();
    if (!folder) { e.termErr.show('Choose the team folder first.'); return; }
    if (!team) { e.termErr.show(R.teams.length ? 'Pick which team to run.' : 'There is no team in that folder yet. Create one on the Teams tab.'); return; }
    const job = e.termJob.value.trim();
    if (job.charAt(0) === '-') { e.termErr.show('Start the job with a word, not a dash.'); e.termJob.focus(); return; }
    await withBusy(e.termBtn, async () => {
      try {
        const res = await api('POST', '/api/terminal', { folder: folder, team: team, job: job || null });
        const board = state.status && state.status.board;
        const bu = board && board.running ? safeUrl(board.url) : null;
        e.termMsg.appendChild(res.opened
          ? notice('ok', h('p', null, job
              ? 'A terminal opened and the team is working on your job. Follow it, approve its steps and read the results on the board. '
              : 'A terminal opened with the team. Type what you want it to do there. ',
              bu ? h('a', { href: bu, target: '_blank', rel: 'noopener noreferrer' }, 'Open the board') : null))
          : notice('info', 'No terminal could be opened here. Copy the command above into your own terminal.'));
        announce(res.opened ? 'Terminal opened.' : 'Could not open a terminal.');
      } catch (err) {
        e.termErr.show(err.message);
      }
    });
  }

  function applyPrefill() {
    const pf = state.prefill || load('tb-prefill');
    if (!pf) return;
    state.prefill = null;
    store('tb-prefill', null);
    if (pf.job) R.els.termJob.value = pf.job;
    if (pf.folder) {
      R.teamPicker.setChosen(pf.folder, true);
      loadTeams(pf.folder, pf.team);
    }
  }

  async function loadTeams(folder, preselect) {
    const e = R.els;
    R.folderErr.clear();
    clear(e.teamsBox);
    R.teams = [];
    try {
      const res = await api('GET', '/api/teams?path=' + encodeURIComponent(folder));
      R.teams = res.teams || [];
      renderTeams(preselect);
    } catch (err) {
      R.folderErr.show(err.message);
    }
  }

  function renderTeams(preselect) {
    const box = R.els.teamsBox;
    clear(box);
    if (!R.teams.length) {
      box.appendChild(notice('info', 'No team here yet: create one on the Teams tab.'));
      updateTerminalCommand();
      return;
    }
    const fs = h('fieldset', null, h('legend', null, 'Which team?'));
    R.teams.forEach((t, i) => {
      const rid = uid('team');
      const gated = t.gated_roles || [];
      const checked = preselect ? t.orchestrator === preselect : R.teams.length === 1 && i === 0;
      fs.appendChild(h('label', { class: 'team-radio', for: rid },
        h('span', { class: 'choice' },
          h('input', { type: 'radio', name: 'run-team', id: rid, value: t.orchestrator, checked: checked, onchange: updateTerminalCommand }),
          h('span', { class: 'choice-text' },
            h('strong', null, t.orchestrator), ' ',
            h('span', { class: 'muted' }, (t.roles || []).length + ' roles'), ' ',
            verdictPill(t.verdict),
            gated.length ? h('small', null, 'These will stop and ask you first: ' + gated.join(', ')) : null,
            (t.board_gated || []).length ? h('small', null, 'Approvals for ' + t.board_gated.join(', ') + ' (web and publishing) appear on the board.') : null,
            t.one_way_flow ? h('small', null, 'One-way flow is on: after files are read, no more web in that session.') : null,
            t.relay_open ? h('small', null, 'Relay open: this team can pass data from a file-reading member to a web member. See the Audit tab.') : null))));
    });
    box.appendChild(fs);
    updateTerminalCommand();
  }

  // ------------------------------------------------------------------------------------------
  // SETTINGS
  // ------------------------------------------------------------------------------------------

  let settingsPicker = null;

  const PROVIDER_TEXT = {
    none: {
      label: 'Built-in templates (recommended — no account, no key)',
      hint: 'Fast and free. Teams are filled in from ready-made templates.',
    },
    openai: {
      label: 'Another AI service',
      hint: 'Any service that speaks the OpenAI chat format. OpenRouter is recommended; you can pick any model it offers, including Claude models, billed by OpenRouter. A local model works too. This only designs the team; it does not run it.',
    },
  };

  function buildSettings(root, data) {
    const S = { data: data };
    const providers = (data.providers && data.providers.length ? data.providers : ['none', 'openai'])
      .filter((p) => PROVIDER_TEXT[p]);

    const envNote = notice('info', 'Your environment already sets these; those values take priority over what you save here.');
    const radios = {};
    const providerFs = h('fieldset', null, h('legend', null, 'How should teams be designed?'));
    providers.forEach((p) => {
      const rid = uid('prov');
      const input = h('input', { type: 'radio', name: 'provider', id: rid, value: p, onchange: syncProvider });
      radios[p] = input;
      providerFs.appendChild(h('div', { class: 'choice' }, input,
        h('label', { class: 'choice-text', for: rid }, PROVIDER_TEXT[p].label, h('small', null, PROVIDER_TEXT[p].hint))));
    });

    const baseUrl = h('input', { type: 'text', id: 'set-base', placeholder: 'https://openrouter.ai/api/v1', autocomplete: 'off', spellcheck: 'false', inputmode: 'url' });
    const model = h('input', { type: 'text', id: 'set-model', autocomplete: 'off', spellcheck: 'false' });
    const key = h('input', { type: 'password', id: 'set-key', autocomplete: 'new-password' });
    const keyState = h('p', { class: 'hint', 'aria-live': 'polite' });
    const removeKey = h('button', { type: 'button', class: 'btn btn-small btn-danger', onclick: onRemoveKey }, 'Remove saved key');
    const openaiBox = h('div', { class: 'card', hidden: true },
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'set-base' }, 'Base URL'), baseUrl),
      h('div', { class: 'field' }, h('label', { class: 'field-label', for: 'set-model' }, 'Model'), model),
      h('div', { class: 'field' },
        h('label', { class: 'field-label', for: 'set-key' }, 'API key'), key,
        h('p', { class: 'hint' }, 'Leave this empty to keep the key you already saved. Stored only in a private file on this computer, and sent only to the service you chose.'),
        keyState, h('div', null, removeKey)));

    const plannerErr = alertBox();
    const plannerSaved = h('span', { class: 'saved', 'aria-live': 'polite' });
    const saveBtn = h('button', { type: 'button', class: 'btn btn-primary', onclick: onSavePlanner }, 'Save');

    const folderCurrent = h('p', { 'aria-live': 'polite' });
    const folderErr = alertBox();
    const folderSaved = h('span', { class: 'saved', 'aria-live': 'polite' });
    settingsPicker = folderPicker({ label: 'Pick a new default folder', allowHome: true });
    const folderSave = h('button', { type: 'button', class: 'btn btn-primary', onclick: () => onSaveFolder(false) }, 'Save');
    const folderClear = h('button', { type: 'button', class: 'btn', onclick: () => onSaveFolder(true) }, 'Clear');

    append(root, [
      h('section', { class: 'card' },
        h('h3', null, 'How teams are designed'),
        envNote,
        providerFs, openaiBox,
        h('p', { class: 'hint' }, 'Whatever a planner suggests is still checked by the same safety rules, and the built-in template is used if it misbehaves.'),
        h('div', { class: 'row' }, saveBtn, plannerSaved),
        plannerErr.el),
      h('section', { class: 'card' },
        h('h3', null, 'Default project folder'),
        folderCurrent,
        settingsPicker.el,
        h('div', { class: 'row step-note' }, folderSave, folderClear, folderSaved),
        folderErr.el),
    ]);

    function syncProvider() {
      const sel = providers.find((p) => radios[p].checked);
      openaiBox.hidden = sel !== 'openai';
    }

    function apply(d) {
      S.data = d;
      const p = d.planner || {};
      const cur = providers.indexOf(p.provider) >= 0 ? p.provider : providers[0];
      radios[cur].checked = true;
      baseUrl.value = p.base_url || '';
      model.value = p.model || '';
      key.value = '';
      keyState.textContent = p.has_key ? 'A key is saved.' : 'No key is saved.';
      removeKey.hidden = !p.has_key;
      envNote.hidden = !d.env_overrides;
      syncProvider();
      clear(folderCurrent);
      append(folderCurrent, ['Current default: ', d.default_folder ? h('code', null, d.default_folder) : h('span', { class: 'muted' }, 'none (pickers start in the top folder)')]);
      if (state.status) {
        state.status.planner = p;
        state.status.default_folder = d.default_folder;
        updatePlannerOption();
      }
    }
    apply(data);

    function flashSaved(el) {
      el.textContent = 'Saved';
      window.setTimeout(() => { el.textContent = ''; }, 4000);
    }

    async function onSavePlanner() {
      plannerErr.clear();
      plannerSaved.textContent = '';
      const sel = providers.find((p) => radios[p].checked) || 'none';
      const body = { provider: sel };
      if (sel === 'openai') {
        body.base_url = baseUrl.value.trim();
        body.model = model.value.trim();
        if (key.value.trim()) body.api_key = key.value.trim();
      }
      await withBusy(saveBtn, async () => {
        try {
          apply(await api('POST', '/api/settings', body));
          flashSaved(plannerSaved);
        } catch (e) {
          plannerErr.show(e.message);
        }
      });
    }

    async function onRemoveKey() {
      plannerErr.clear();
      await withBusy(removeKey, async () => {
        try {
          apply(await api('POST', '/api/settings', { api_key: '' }));
          flashSaved(plannerSaved);
        } catch (e) {
          plannerErr.show(e.message);
        }
      });
    }

    async function onSaveFolder(clearIt) {
      folderErr.clear();
      folderSaved.textContent = '';
      let value = '';
      if (!clearIt) {
        value = settingsPicker.getChosen() || '';
        if (!value) { folderErr.show('Choose a folder first, then press Save.'); return; }
      }
      await withBusy([folderSave, folderClear], async () => {
        try {
          apply(await api('POST', '/api/settings', { default_folder: value }));
          flashSaved(folderSaved);
        } catch (e) {
          folderErr.show(e.message);
        }
      });
    }
  }

  // ------------------------------------------------------------------------------------------
  // Start up
  // ------------------------------------------------------------------------------------------

  function retryBox(root, message, retry) {
    clear(root);
    const btn = h('button', { type: 'button', class: 'btn', onclick: retry }, 'Try again');
    root.appendChild(h('div', { class: 'alert alert-error', role: 'alert' }, h('p', null, message), btn));
  }

  async function startCreate() {
    const root = $('#create-body');
    try {
      const data = await api('GET', '/api/blueprints');
      state.blueprints = data.blueprints;
      state.legs = data.legs;
      clear(root);
      buildCreate(root, data);
    } catch (e) {
      retryBox(root, e.message, startCreate);
    }
  }

  async function startSettings() {
    const root = $('#settings-body');
    try {
      const data = await api('GET', '/api/settings');
      clear(root);
      buildSettings(root, data);
      if (state.current === 'settings' && settingsPicker) settingsPicker.ensureLoaded();
    } catch (e) {
      retryBox(root, e.message, startSettings);
    }
  }

  async function init() {
    buildGuard($('#guard-body'));
    buildAudit($('#audit-body'));
    buildRun($('#run-body'));
    window.addEventListener('hashchange', route);
    await refreshStatus();
    startCreate();
    startSettings();
    route();
  }

  init();
}());
