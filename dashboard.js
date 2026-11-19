/* Tableau de bord Assistant — aucune dépendance.
   Sécurité : tout contenu venant de messages externes est inséré avec textContent (jamais innerHTML). */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const mem = {};
  const kv = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return mem[k] || null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { mem[k] = v; } },
  };

  // ---------- Jeton ----------
  let token = kv.get('assistant.token') || '';
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get('t')) {
    token = hash.get('t');
    kv.set('assistant.token', token);
    history.replaceState(null, '', location.pathname);
  }

  // ---------- API ----------
  class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }

  async function api(path, opts = {}) {
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || 'GET',
        headers: { 'X-Admin-Token': token, 'Content-Type': 'application/json' },
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        cache: 'no-store',
      });
    } catch (_) {
      throw new ApiError('Serveur injoignable', 0);
    }
    if (res.status === 401) { showAuth(); throw new ApiError('Jeton invalide', 401); }
    let data = null;
    try { data = await res.json(); } catch (_) { /* corps vide */ }
    if (!res.ok) throw new ApiError((data && data.error) || `Erreur ${res.status}`, res.status);
    return data;
  }

  // ---------- Petits utilitaires ----------
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c) node.append(c);
    return node;
  }
  function icon(id) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ic');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + id);
    svg.append(use);
    return svg;
  }
  function ago(ts) {
    if (!ts) return '';
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 45) return "à l'instant";
    if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
    if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
    return new Date(ts * 1000).toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
  }
  let toastTimer = 0;
  function toast(msg, isError) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' err' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
  }
  function setText(node, value) { if (node && node.textContent !== value) node.textContent = value; }

  // ---------- Navigation ----------
  const TABS = ['home', 'drafts', 'test', 'activity'];
  let view = 'home';
  function show(name) {
    if (!TABS.includes(name)) name = 'home';
    view = name;
    $$('.view').forEach(v => { v.hidden = v.dataset.view !== name; });
    $$('.tab').forEach(t => { t.setAttribute('aria-current', t.dataset.tab === name ? 'page' : 'false'); });
    $('#dock').style.setProperty('--i', String(TABS.indexOf(name)));
    $('.dock-inner').style.setProperty('--i', String(TABS.indexOf(name)));
    try { sessionStorage.setItem('assistant.view', name); } catch (_) { /* ignoré */ }
    window.scrollTo({ top: 0 });
  }
  $$('.tab').forEach(t => t.addEventListener('click', () => show(t.dataset.tab)));

  // ---------- Tilt 3D (souris uniquement, jamais sur tactile) ----------
  const canTilt = matchMedia('(hover: hover) and (pointer: fine)').matches &&
                  !matchMedia('(prefers-reduced-motion: reduce)').matches;
  function bindTilt(node) {
    if (!canTilt || node.dataset.tilt) return;
    node.dataset.tilt = '1';
    let raf = 0, rect = null;
    node.addEventListener('pointermove', e => {
      if (e.pointerType !== 'mouse') return;
      if (!rect) rect = node.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width, y = (e.clientY - rect.top) / rect.height;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        node.style.setProperty('--ry', ((x - .5) * 9).toFixed(2) + 'deg');
        node.style.setProperty('--rx', ((.5 - y) * 9).toFixed(2) + 'deg');
        node.style.setProperty('--mx', (x * 100).toFixed(1) + '%');
        node.style.setProperty('--my', (y * 100).toFixed(1) + '%');
        node.classList.add('is-tilting');
      });
    });
    node.addEventListener('pointerleave', () => {
      rect = null; cancelAnimationFrame(raf);
      ['--rx', '--ry'].forEach(p => node.style.setProperty(p, '0deg'));
      node.classList.remove('is-tilting');
    });
    window.addEventListener('scroll', () => { rect = null; }, { passive: true });
  }
  $$('.tilt').forEach(bindTilt);

  // ---------- Rendu ----------
  const KINDS = {
    replied: ['Réponse envoyée', 'ok'], sent: ['Brouillon envoyé', 'ok'], draft: ['Brouillon prêt', 'info'],
    escalated: ['À toi de jouer', 'warn'], ignored: ['Ignoré', ''], skipped: ['Hors absence', ''],
    error: ['Erreur', 'err'], info: ['Info', 'info'], discarded: ['Brouillon écarté', ''],
  };
  const CHANNELS = { email: 'Email', whatsapp: 'WhatsApp', system: 'Système' };
  let lastState = null;

  function setSeg(id, value, disabled) {
    $$(`#${id} button`).forEach(b => {
      b.setAttribute('aria-checked', String(b.dataset.v === value));
      b.disabled = !!disabled;
    });
  }

  function renderHero(s) {
    const hero = $('#hero');
    const active = s.away.active;
    hero.classList.toggle('is-on', active);
    const title = $('#hero-title');
    setText(title, active ? 'ACTIF' : 'EN VEILLE');
    title.classList.toggle('off', !active);
    const sub = active ? 'Ton assistant répond à ta place.' : 'Tu es dispo : l’assistant n’intervient pas.';
    const extra = s.away.override === 'auto' ? ` Plage auto : ${s.away.window}.`
                : s.away.override === 'on' ? ' Forcé : toujours absent.' : ' Forcé : jamais absent.';
    setText($('#hero-sub'), sub + extra);
    const warn = $('#hero-warn');
    if (!s.groq_configured) { warn.hidden = false; setText(warn, '⚠ Clé GROQ_API_KEY absente : aucune réponse possible.'); }
    else warn.hidden = true;
    setSeg('seg-away', s.away.override);
    setSeg('seg-mode', s.reply_mode);
  }

  function renderConnectors(list) {
    for (const c of list) {
      const card = $(`[data-conn="${c.id}"]`);
      if (!card) continue;
      const led = $('[data-f="led"]', card);
      let cls = 'led', status;
      if (!c.configured) { status = 'Non connecté'; }
      else if (!c.enabled) { status = 'Désactivé dans config.json'; cls += ' warn'; }
      else if (c.err && (!c.ok_ts || (c.err_ts || 0) >= c.ok_ts)) { status = 'Erreur : ' + c.err; cls += ' err'; }
      else if (c.ok_ts) { status = 'Connecté · activité ' + ago(c.ok_ts); cls += ' ok'; }
      else { status = 'Configuré · en attente du premier signal'; cls += ' warn'; }
      led.className = cls;
      setText($('[data-f="status"]', card), status);
      setText($('[data-f="detail"]', card), c.detail || '—');
      const hint = $('[data-f="hint"]', card);
      hint.hidden = !c.hint;
      setText(hint, c.hint || '');
    }
  }

  const draftNodes = new Map();

  function buildDraft(d) {
    const ta = el('textarea', { rows: '5', maxlength: '4000', 'aria-label': 'Réponse proposée' });
    ta.value = d.reply;
    const time = el('span', { class: 'time' });
    const sendBtn = el('button', { class: 'btn primary', type: 'button' }, icon('i-send'), document.createTextNode('Envoyer'));
    const dropBtn = el('button', { class: 'btn', type: 'button' }, icon('i-x'), document.createTextNode('Ignorer'));
    const card = el('article', { class: 'draft glass-lite', 'data-id': d.id },
      el('div', { class: 'draft-head' },
        el('span', { class: 'chip' }, icon(d.channel === 'email' ? 'i-mail' : 'i-chat')),
        el('div', {}, el('strong', { text: d.name }), el('div', { class: 'muted small', text: d.sender + (d.subject ? ' · ' + d.subject : '') })),
        time),
      el('div', { class: 'quote', text: d.incoming }),
      d.note ? el('p', { class: 'note', text: '⚠ ' + d.note }) : null,
      ta,
      el('div', { class: 'actions' }, dropBtn, sendBtn));
    card._time = time;

    const lock = (v) => { sendBtn.disabled = v; dropBtn.disabled = v; ta.disabled = v; };
    sendBtn.addEventListener('click', async () => {
      lock(true);
      try { await api(`/api/drafts/${d.id}/send`, { method: 'POST', body: { text: ta.value } }); toast('Message envoyé ✓'); await refresh(); }
      catch (e) { toast(e.message, true); lock(false); }
    });
    dropBtn.addEventListener('click', async () => {
      lock(true);
      try { await api(`/api/drafts/${d.id}/discard`, { method: 'POST', body: {} }); toast('Brouillon écarté'); await refresh(); }
      catch (e) { toast(e.message, true); lock(false); }
    });
    return card;
  }

  // Réconciliation par id : les textareas en cours d'édition ne sont JAMAIS recréées.
  function renderDrafts(list) {
    const box = $('#drafts');
    const ids = new Set(list.map(d => d.id));
    for (const [id, node] of draftNodes) if (!ids.has(id)) { node.remove(); draftNodes.delete(id); }
    list.forEach((d, i) => {
      let node = draftNodes.get(d.id);
      if (!node) { node = buildDraft(d); draftNodes.set(d.id, node); }
      if (box.children[i] !== node) box.insertBefore(node, box.children[i] || null);
      setText(node._time, ago(d.created));
    });
    $('#drafts-empty').hidden = list.length > 0;
    const badge = $('#badge');
    badge.hidden = list.length === 0;
    setText(badge, String(list.length));
    document.title = (list.length ? `(${list.length}) ` : '') + 'Assistant · Glass';
  }

  function renderEvents(list) {
    const ol = $('#events');
    ol.hidden = list.length === 0;
    $('#events-empty').hidden = list.length > 0;
    const frag = document.createDocumentFragment();
    for (const e of list) {
      const [label, cls] = KINDS[e.kind] || [e.kind, ''];
      frag.append(el('li', {},
        el('span', { class: 'dot ' + cls }),
        el('div', {},
          el('div', { class: 'ev-title', text: `${label}${e.who ? ' · ' + e.who : ''} · ${CHANNELS[e.channel] || e.channel}` }),
          e.text ? el('div', { class: 'ev-text', text: e.text }) : null),
        el('span', { class: 'ev-time', text: ago(e.ts) })));
    }
    ol.replaceChildren(frag);
  }

  function render(s) {
    lastState = s;
    setText($('#owner'), s.owner || '—');
    setText($('#model-pill'), s.model || '—');
    renderHero(s);
    setText($('#st-replied'), String(s.stats.replied));
    setText($('#st-pending'), String(s.stats.pending));
    setText($('#st-escalated'), String(s.stats.escalated));
    renderConnectors(s.connectors);
    renderDrafts(s.drafts);
    renderEvents(s.events);
  }

  // ---------- Rafraîchissement ----------
  let inflight = false, fails = 0;
  async function refresh() {
    if (inflight || !token) return;
    inflight = true;
    try {
      render(await api('/api/state'));
      fails = 0;
      $('#banner').hidden = true;
    } catch (e) {
      if (e.status !== 401 && ++fails >= 2) $('#banner').hidden = false;
    } finally { inflight = false; }
  }
  setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });

  // ---------- Contrôles ----------
  $('#seg-away').addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    setSeg('seg-away', b.dataset.v, true);
    try { await api('/api/away', { method: 'POST', body: { state: b.dataset.v } }); await refresh(); }
    catch (err) { toast(err.message, true); if (lastState) setSeg('seg-away', lastState.away.override); }
  });
  $('#seg-mode').addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    if (b.dataset.v === 'auto' && !confirm('Passer en réponse automatique ?\nL’assistant enverra les messages sans ta validation (sauf liens et numéros).')) return;
    setSeg('seg-mode', b.dataset.v, true);
    try { await api('/api/mode', { method: 'POST', body: { mode: b.dataset.v } }); await refresh(); }
    catch (err) { toast(err.message, true); if (lastState) setSeg('seg-mode', lastState.reply_mode); }
  });

  // ---------- Simulateur ----------
  $('#test-form').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = $('#test-btn');
    btn.disabled = true;
    try {
      const r = await api('/api/test', { method: 'POST', body: {
        text: $('#test-text').value, name: $('#test-name').value, channel: $('#test-channel').value } });
      const labels = { reply: 'Répond', escalate: 'Te prévient', ignore: 'Ignore' };
      const tag = $('#res-tag');
      tag.className = 'tag ' + r.action;
      setText(tag, labels[r.action] || r.action);
      setText($('#res-outcome'), r.outcome);
      const reply = $('#res-reply');
      reply.hidden = !r.reply;
      setText(reply, r.reply || '');
      setText($('#res-reason'), r.reason ? 'Raison : ' + r.reason : '');
      $('#test-result').hidden = false;
    } catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; }
  });

  // ---------- Connexion ----------
  function showAuth() { $('#auth').hidden = false; }
  $('#auth-form').addEventListener('submit', e => {
    e.preventDefault();
    token = $('#auth-token').value.trim();
    kv.set('assistant.token', token);
    $('#auth').hidden = true;
    refresh();
  });

  // ---------- Démarrage ----------
  let start = 'home';
  try { start = sessionStorage.getItem('assistant.view') || 'home'; } catch (_) { /* ignoré */ }
  show(start);
  if (!token) showAuth(); else refresh();
})();
