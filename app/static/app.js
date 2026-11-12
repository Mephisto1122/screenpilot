'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const W = 1280, H = 800;
const NAMES = {private:'Private', smart:'Smart & private', fast:'Fastest', openai_text:'GPT, text only', anthropic_text:'Claude, text only', anthropic_direct:'Claude', openai_direct:'GPT', local:'General model on this Mac', anthropic:'Claude plans, Mac acts', openai:'GPT plans, Mac acts', uitars:'UI-TARS', anthropic_uitars:'Claude + UI-TARS'};

let state = null, chatRev = -1, chat = {messages:[], memory:[], telegram:{}}, settings = null, room = null;
let firstChat = true, runCost = null, costTimer = 0;
let planner = localStorage.getItem('planner-v7') ? localStorage.getItem('planner') : null, driving = false, lastAction = 0, sending = false, toastTimer;
// Workers: each has its own desktop and chat. `sel` is the one on screen; only it streams video.
let workers = [{id: 1, status: 'idle', steps: 0, queued: 0}], sel = +(localStorage.getItem('worker') || 1), sharedQueue = 0, maxWorkers = 6;
let target = localStorage.getItem('target') === 'any' ? 'any' : 'this';
const wpath = (p, id = sel) => `w/${id}/${p}`;

// Every request has a time limit: one that stalls (the Mac slept, the container restarted) must not freeze the page.
async function api(path, body){
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), body === undefined ? 15000 : 60000);
  let r;
  try { r = await fetch('/api/' + path, {signal: ctl.signal, ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Screenpilot':'1'}, body:JSON.stringify(body)})}) }
  catch { throw Error('Screenpilot is not answering. Is it running?') }
  finally { clearTimeout(timer) }
  let data; try { data = await r.json() } catch { throw Error('Screenpilot did not answer. Is it running?') }
  if (!r.ok) throw Error(data.error || `Request failed (${r.status})`);
  return data;
}
function toast(msg){ const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => t.hidden = true, 4500) }
async function act(fn){ try { await fn() } catch (e) { toast(e.message) } }
const clock = s => { s = Math.max(0, Math.round(s)); return s < 60 ? `${s}s` : `${Math.floor(s/60)}m ${String(s%60).padStart(2,'0')}s` };
const timeOf = t => new Date(t*1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});

/* ---------- screens: one slide per worker; swipe, scroll sideways or press [ ] to move between them ---------- */
const deck = $('#deck');
const slides = new Map();  // worker id -> {el, img, thumb}
function syncDeck(){
  const ids = workers.map(w => w.id);
  for (const [id, s] of slides) if (!ids.includes(id)) { s.el.remove(); if (s.thumb) URL.revokeObjectURL(s.thumb); slides.delete(id) }
  ids.forEach((id, i) => {
    let s = slides.get(id);
    if (!s) {
      const el = document.createElement('div'); el.className = 'slide'; el.dataset.w = id;
      const im = document.createElement('img'); im.alt = `Worker ${id}'s desktop`; im.draggable = false; el.append(im);
      s = {el, img: im, thumb: null}; slides.set(id, s);
    }
    if (deck.children[i] !== s.el) deck.insertBefore(s.el, deck.children[i] || null);
  });
}
// Live video only for the worker on screen; the others show a recent still, so many workers stay cheap.
function stream(){
  for (const [id, s] of slides) if ((id !== sel || document.hidden) && s.img.dataset.live) { delete s.img.dataset.live; s.img.onerror = null; s.img.removeAttribute('src') }
  const s = slides.get(sel); if (!s || document.hidden || s.img.dataset.live) return;
  const id = sel; s.img.dataset.live = '1';
  s.img.onerror = () => { s.img.onerror = null; pollFrames(id) };
  s.img.src = `/api/w/${id}/stream?t=${Date.now()}`;
}
function pollFrames(id){
  const next = () => {
    const s = slides.get(id); if (id !== sel || !s?.img.dataset.live || document.hidden) return;
    const i = new Image(); i.onload = () => { if (id === sel) s.img.src = i.src; setTimeout(next, 350) }; i.onerror = () => setTimeout(next, 1500);
    i.src = `/api/w/${id}/screen?t=${Date.now()}`;
  };
  next();
}
document.addEventListener('visibilitychange', () => { stream(); if (!document.hidden) { loadThumbs(); refresh() } });
const liveImg = () => slides.get(sel)?.img;

// Small pictures of every worker's screen, for the rail and for the slides that are not on screen.
let thumbsBusy = false;
async function loadThumbs(){
  if (thumbsBusy || document.hidden || workers.length < 2) return;
  thumbsBusy = true;
  for (const w of workers) {  // one at a time, so the browser keeps connections free for the live screen
    try {
      const r = await fetch(`/api/w/${w.id}/thumb`); if (!r.ok) continue;
      const url = URL.createObjectURL(await r.blob()), s = slides.get(w.id);
      if (!s) { URL.revokeObjectURL(url); continue }
      const old = s.thumb; s.thumb = url; s.el.style.backgroundImage = `url(${url})`;
      const shot = $(`#rail-list [data-w="${w.id}"] .chip-shot`); if (shot) shot.style.backgroundImage = `url(${url})`;
      if (old) setTimeout(() => URL.revokeObjectURL(old), 2000);
    } catch {}
  }
  thumbsBusy = false;
}

function select(id, scroll = 'smooth'){
  if (!slides.has(id)) return;
  const changed = id !== sel;
  sel = id; try { localStorage.setItem('worker', id) } catch {}
  if (scroll) scrollToSelected(scroll);
  stream();
  if (!changed) return;
  state = null; chatRev = -1; firstChat = true; lastAction = 0; runCost = null; chat = {...chat, messages: []};
  if (driving) setDriving(false);
  if ($('#ctl-eyes').getAttribute('aria-pressed') === 'true') { $('#marks').removeAttribute('src'); loadMarks() }
  // Clear the old conversation at once; the new one arrives with the next refresh.
  $('#thread').querySelectorAll('.msg').forEach(n => n.remove()); $('#empty').hidden = true; $('#turn').hidden = true;
  renderRail(); refresh();
}
function scrollToSelected(behavior){
  const i = workers.findIndex(w => w.id === sel);
  if (i >= 0 && Math.abs(deck.scrollLeft - i * deck.clientWidth) > 2) deck.scrollTo({left: i * deck.clientWidth, behavior});
}
function step(by){ const i = workers.findIndex(w => w.id === sel); const w = workers[i + by]; if (w) select(w.id) }
// When a swipe or sideways scroll comes to rest, the worker it landed on becomes the selected one.
let deckTimer;
deck.addEventListener('scroll', () => {
  clearTimeout(deckTimer);
  deckTimer = setTimeout(() => { const w = workers[Math.round(deck.scrollLeft / deck.clientWidth)]; if (w && w.id !== sel) select(w.id, null) }, 110);
}, {passive: true});
new ResizeObserver(() => scrollToSelected('instant')).observe(deck);
document.addEventListener('keydown', e => {
  const t = e.target, typing = t.closest?.('input,textarea,select,[contenteditable]');
  if (driving || typing || e.metaKey || e.ctrlKey || e.altKey || !$('#sheet').hidden || workers.length < 2) return;
  if (e.key === '[' || (e.key === 'ArrowLeft' && t === $('#screen'))) { e.preventDefault(); step(-1) }
  else if (e.key === ']' || (e.key === 'ArrowRight' && t === $('#screen'))) { e.preventDefault(); step(1) }
  else if (/^[1-9]$/.test(e.key) && slides.has(+e.key)) { e.preventDefault(); select(+e.key) }
});

/* ---------- the rail: every worker at a glance ---------- */
function chipState(w){
  const q = w.queued ? ` · ${w.queued} queued` : '';
  if (w.status === 'waiting') return ['turn', ({approval: 'Needs approval', code: 'Needs a code', stuck: 'Needs your help', question: 'Has a question'}[w.waiting?.type] || 'Your turn') + q];
  if (w.status === 'running') return ['working', `Working · ${w.steps} action${w.steps === 1 ? '' : 's'}${q}`];
  if (w.queued) return ['working', 'Starting' + q];
  return ['idle', {completed: 'Done', failed: 'Could not finish', stopped: 'Stopped'}[w.status] || 'Ready'];
}
function renderRail(){
  const many = workers.length > 1, full = workers.length >= maxWorkers;
  $('#rail').hidden = !many;
  document.documentElement.style.setProperty('--rail-h', many ? '50px' : '0px');
  $('#ctl-new').hidden = many || full; $('#rail-add').hidden = full;
  $('#ctl-close').hidden = sel === 1 || !many;
  $('#target').hidden = !many; renderTarget();
  $('#screen').title = many ? 'Swipe, scroll sideways or press [ and ] to move between workers' : '';
  if (!many) return;
  const list = $('#rail-list');
  workers.forEach((w, i) => {
    let chip = list.querySelector(`[data-w="${w.id}"]`);
    if (!chip) {
      chip = document.createElement('button'); chip.className = 'chip'; chip.dataset.w = w.id;
      chip.innerHTML = `<span class="chip-shot"></span><span class="chip-text"><b>Worker ${w.id}</b><small></small></span>`;
      const thumb = slides.get(w.id)?.thumb; if (thumb) chip.firstChild.style.backgroundImage = `url(${thumb})`;
    }
    if (list.children[i] !== chip) list.insertBefore(chip, list.children[i] || null);
    const [st, text] = chipState(w);
    chip.dataset.state = st; chip.querySelector('small').textContent = text;
    chip.setAttribute('aria-current', String(w.id === sel)); chip.title = (w.task && w.status !== 'idle' ? w.task + ' · ' : '') + `Press ${w.id}`;
  });
  [...list.children].forEach(c => { if (!slides.has(+c.dataset.w)) c.remove() });
  const active = list.querySelector('[aria-current=true]'); if (active) active.scrollIntoView({block: 'nearest', inline: 'nearest'});
  $('#rail-note').textContent = sharedQueue ? `${sharedQueue} waiting for a free worker` : '';
  $('#rail-stop').hidden = workers.filter(w => w.status === 'running' || w.status === 'waiting' || w.queued).length + sharedQueue < 2;
}
$('#rail-list').addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) select(+c.dataset.w) });

const asked = new Set();
function setWorkers(r){
  const before = workers.map(w => w.id).join();
  workers = r.workers; sharedQueue = r.shared_queue || 0; maxWorkers = r.max || maxWorkers;
  if (r.version) $('#version').textContent = 'Screenpilot ' + r.version;
  if (workers.map(w => w.id).join() !== before) { syncDeck(); scrollToSelected('instant') }
  if (!slides.has(sel)) select(1, 'instant');
  // Someone else needs you: say so once, even while you watch another worker.
  for (const w of workers) {
    const key = `${w.id}:${w.waiting?.reason || ''}`;
    if (w.status === 'waiting' && w.id !== sel && !asked.has(key)) { asked.add(key); toast(`Worker ${w.id} needs you. Press ${w.id} to see it.`) }
  }
  renderRail();
}
async function refreshWorkers(){ try { setWorkers(await api('workers')) } catch {} }
async function addWorker(){
  await act(async () => {
    const r = await api('workers', {}); setWorkers(r); select(r.id);
    toast(`Worker ${r.id} has its own desktop. Give it a task below.`); loadThumbs();
  });
}
$('#rail-add').addEventListener('click', addWorker);
$('#ctl-new').addEventListener('click', addWorker);
$('#rail-stop').addEventListener('click', () => act(async () => { await api('stop', {}); toast('Stopped every worker'); refreshWorkers() }));
// Removing needs a second click, like Reset.
let closeArmed = 0;
$('#ctl-close').addEventListener('click', () => {
  if (Date.now() - closeArmed > 3500) {
    closeArmed = Date.now(); $('#ctl-close').classList.add('confirm'); $('#ctl-close-text').textContent = `Click again to remove worker ${sel}`;
    setTimeout(() => { if (Date.now() - closeArmed >= 3400) { $('#ctl-close').classList.remove('confirm'); $('#ctl-close-text').textContent = 'Remove worker' } }, 3500);
    return;
  }
  closeArmed = 0; $('#ctl-close').classList.remove('confirm'); $('#ctl-close-text').textContent = 'Remove worker';
  const gone = sel, prev = workers[Math.max(0, workers.findIndex(w => w.id === gone) - 1)].id;
  act(async () => { select(prev); setWorkers(await api('workers/remove', {id: gone})); toast(`Worker ${gone} removed`) });
});

function renderTarget(){ $('#target').textContent = target === 'any' ? 'Any free worker' : `Worker ${sel}`; $('#target').dataset.mode = target }
$('#target').addEventListener('click', () => { target = target === 'any' ? 'this' : 'any'; try { localStorage.setItem('target', target) } catch {} renderTarget() });

function ripple(x, y){
  const r = document.createElement('span'); r.className = 'ripple';
  r.style.left = (x / W * 100) + '%'; r.style.top = (y / H * 100) + '%';
  $('#ripples').append(r); setTimeout(() => r.remove(), 1200);
}
let captionTimer;
function caption(text){ const c = $('#caption'); c.textContent = text; c.classList.add('show'); clearTimeout(captionTimer); captionTimer = setTimeout(() => c.classList.remove('show'), 1800) }

function showAction(a){
  if (!a || a.t <= lastAction) return;
  const fresh = lastAction !== 0; lastAction = a.t;
  if (!fresh) return;
  if ((a.kind === 'click' || a.kind === 'double_click') && a.x != null) { ripple(a.x, a.y); if (a.label && !/^click/i.test(a.label)) caption(a.label) }
  else if (a.kind === 'type') caption(a.text ? `Typing “${a.text}”` : 'Typing')
  else if (a.kind === 'key') caption('Pressing ' + a.key)
  else if (a.kind === 'scroll') caption('Scrolling')
}

/* ---------- mode: one word for what is happening, always visible ---------- */
function modeOf(s){
  if (!s.desktop.available) return ['offline', 'Desktop offline', ''];
  const a = s.agent;
  if (a.status === 'waiting') return ['turn', ({approval: 'Needs your approval', code: 'Needs a code from your phone', stuck: 'Stuck · needs your help', question: 'A question before starting'}[a.waiting?.type] || 'Your turn'), ''];
  if (a.status === 'running') {
    const step = a.skill ? `saved skill · step ${a.skill.at} of ${a.skill.of}` : a.plan?.length > 1 ? `step ${Math.min(a.goal_index + 1, a.plan.length)} of ${a.plan.length}` : `${a.steps} actions`;
    // While it waits for something (a model, the Mac's look, a command), say what and for how long.
    const now = Date.now()/1000, waitingFor = a.request_started_at && a.message ? `${a.message.replace(/\s*\(\d+ characters\)/, '')} ${clock(now - a.request_started_at)}` : '';
    return ['working', 'Working', `${waitingFor || step} · ${clock(now - a.started_at)}${runCost != null ? ' · $' + runCost.toFixed(2) : ''}`];
  }
  if (driving) return ['driving', "You're driving", ''];
  if (s.queued) return ['working', 'Starting', `${s.queued} queued`];
  return ['idle', 'Ready', NAMES[planner] || ''];
}

function render(){
  if (!state) return;
  const s = state, a = s.agent;
  const [mode, text, meta] = modeOf(s);
  $('#mode').dataset.state = mode; $('#mode-text').textContent = text; $('#mode-meta').textContent = meta;
  $('#screen').dataset.state = mode === 'working' || mode === 'turn' ? mode : 'idle';
  $('#screen-offline').hidden = s.desktop.available;
  if (!s.desktop.available) $('#screen-offline').innerHTML = `<p>The desktop isn't running.</p><p class="sub">${esc(s.desktop.error || '')} Start Screenpilot with <code>./run.sh</code>.</p>`;
  const busy = a.status === 'running' || a.status === 'waiting' || s.queued > 0;
  $('#ctl-stop').disabled = !busy;
  renderLive();
  $('#takeover').disabled = a.status === 'running';
  if (a.status === 'running' && driving) setDriving(false);

  // your turn, on the screen itself
  const w = a.status === 'waiting' ? a.waiting : null;
  // The message box always talks to the agent: an answer when it asks, a correction while it works, a task otherwise.
  input.placeholder = w ? ({approval: 'Yes or no, or say what to do instead', code: 'Type the code here', stuck: 'Tell the agent what to do', question: 'Your answer'}[w.type] || 'Write "done" when you have finished')
    : a.status === 'running' ? 'Tell the agent something: a correction, a detail, a change' : 'Give the agent a task';
  $('#turn').hidden = !w;
  if (w) {
    const question = w.type === 'question';
    const approval = w.type === 'approval', code = w.type === 'code', stuck = w.type === 'stuck' || question;  // both are answered in words
    $('#turn-title').textContent = approval ? 'Approve this?' : code ? 'Code needed' : question ? 'Before I start' : stuck ? 'I need your help' : 'Your turn';
    $('#turn-help').placeholder = question ? 'Your answer' : 'Tell me what to do (optional)'; turnIdle = question ? 'Start anyway' : 'Try again';
    $('#turn-reason').textContent = w.reason + (approval || code || stuck || driving ? '' : ' Take over to do it, then press I\'m done.');
    $('#turn-continue').hidden = approval || code || stuck; $('#turn-approve').hidden = !approval; $('#turn-deny').hidden = !approval;
    $('#turn').classList.toggle('stuck', stuck);
    const helpWasHidden = $('#turn-help-form').hidden;
    $('#turn-help-form').hidden = !stuck; $('#turn-stop').hidden = !stuck;
    if (stuck && helpWasHidden) $('#turn-help').value = '';
    $('#turn-help-send').textContent = $('#turn-help').value.trim() ? 'Send' : turnIdle;
    const wasHidden = $('#turn-code-form').hidden;
    $('#turn-code-form').hidden = !code; $('#turn-no-code').hidden = !code;
    if (code && wasHidden) { $('#turn-code').value = ''; $('#turn-code').focus() }
  }

  // the goal, as dots on a line
  const plan = a.plan || [], active = (a.status === 'running' || a.status === 'waiting');
  $('#goal').hidden = !(active || (a.status === 'completed' && plan.length));
  document.documentElement.style.setProperty('--goal-h', $('#goal').hidden ? '0px' : (plan.length > 1 ? '100px' : '46px'));
  $('#goal-text').textContent = a.title || a.task || '';
  $('#steps').hidden = plan.length < 2;
  $('#steps').innerHTML = plan.length < 2 ? '' : plan.map((g, i) => {
    const cls = a.status === 'completed' || i < a.goal_index ? 'done' : i === a.goal_index ? 'now' : '';
    return `<li class="${cls}" title="${esc(g)}"><span>${esc(g)}</span></li>`;
  }).join('');

  showAction(a.last_action);
  renderPop();
}

function renderPop(){
  if (!state) return;
  const a = state.agent;
  $('#pop-task').textContent = a.title || a.task || 'Nothing running. Give the agent a task below.';
  $('#pop-model').textContent = NAMES[a.planner] || NAMES[planner] || '–';
  $('#pop-steps').textContent = a.task ? (a.max_steps ? `${a.steps} of ${a.max_steps}` : `${a.steps}`) : '–';
  $('#pop-time').textContent = a.started_at ? clock((a.status === 'running' || a.status === 'waiting' ? Date.now()/1000 : (a.ended_at || Date.now()/1000)) - a.started_at) : '–';
  $('#pop-note').textContent = a.note || a.message || '';
}

/* ---------- conversation ---------- */
/* Text in the chat: the agent and the user write plain text; this gives it structure that is easy to read (numbered
   results with their details, lists, small headings, links) without trusting any of it as HTML. */
const LINK = /(https?:\/\/[^\s<]+|(?<![@\w.])(?:[a-z0-9-]+\.)+(?:com|org|net|io|co|ai|app|dev|me|tv|gov|edu|il|uk|de|fr)(?:\/[^\s<]*)?)/gi;
const LABEL = /^([A-Z][\w '’&/-]{0,24}?):\s+(\S.*)$/;
function inline(raw){
  return String(raw).split(LINK).map((part, i) => {
    if (i % 2 === 0) return esc(part).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
    const tail = (part.match(/[.,;:!?)\]'"]+$/) || [''])[0], url = tail ? part.slice(0, -tail.length) : part;
    return `<a href="${esc(/^https?:/i.test(url) ? url : 'https://' + url)}" target="_blank" rel="noopener noreferrer">${esc(url.replace(/^https?:\/\/(www\.)?/i, '').replace(/\/$/, ''))}</a>${esc(tail)}`;
  }).join('');
}
function rich(text){
  // Plain text in, structure out: paragraphs, lists with details, facts (Label: value), headings, tables, quotes, code.
  // Every block gets dir="auto", so Hebrew or Arabic reads right to left next to English.
  const out = []; let para = [], list = null;
  const short = t => { const m = LABEL.exec(t); return m && m[1].split(' ').length <= 3 && m[1].length <= 28 ? m : null };   // "Brands: ...", not a sentence with a colon
  const named = t => { const m = short(t); return m ? `<span class="rt-name">${esc(m[1])}:</span> ${inline(m[2])}` : inline(t) };
  const fact = t => { const m = short(t); return `<div class="rt-row"><span class="rt-k">${esc(m[1])}</span><span dir="auto">${inline(m[2])}</span></div>` };
  const detail = t => short(t) ? fact(t) : `<div class="rt-sub" dir="auto">${inline(t)}</div>`;
  const endPara = () => {
    if (!para.length) return;
    // Two or more "Label: value" lines in a row are facts: shown in two columns.
    out.push(para.length > 1 && para.every(short) ? `<div class="rt-facts">${para.map(fact).join('')}</div>` : `<p dir="auto">${para.map(inline).join('<br>')}</p>`); para = [];
  };
  const endList = () => {
    if (!list) return;
    out.push(`<${list.type} class="rt-list${list.items.some(it => it.subs.length) ? ' roomy' : ''}">` + list.items.map(it => {
      const box = /^\[( |x|X)\]\s+(.*)$/.exec(it.head);   // - [ ] to do, - [x] done
      return `<li dir="auto"${it.n ? ` value="${it.n}"` : ''}${box ? ` class="check${box[1] === ' ' ? '' : ' done'}"` : ''}>${named(box ? box[2] : it.head)}${it.subs.map(detail).join('')}</li>` }).join('') + `</${list.type}>`);
    list = null;
  };
  const item = (type, head, n) => { endPara(); if (!list || list.type !== type) { endList(); list = {type, items: []} } list.items.push({head, n, subs: []}); list.gap = false };
  const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  const lines = String(text).replace(/\r/g, '').split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], t = line.trim(), indented = /^(\s{2,}|\t)/.test(line); let m;
    if (/^```/.test(t)) {   // code, kept exactly as written
      endPara(); endList(); const code = []; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) code.push(lines[i++]);
      out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); continue }
    if (/^\|.*\|/.test(t)) {   // a table
      endPara(); endList(); const rows = [];
      while (i < lines.length && /^\|.*\|/.test(lines[i].trim())) rows.push(cells(lines[i++])); i--;
      const head = rows.length > 1 && rows[1].every(c => /^:?-{2,}:?$/.test(c)) ? rows.shift() : null; if (head) rows.shift();
      out.push(`<div class="rt-table"><table>${head ? `<thead><tr>${head.map(c => `<th dir="auto">${inline(c)}</th>`).join('')}</tr></thead>` : ''}<tbody>${rows.map(r => `<tr>${r.map(c => `<td dir="auto">${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`); continue }
    if (!t) { endPara(); if (list) list.gap = true; continue }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { endPara(); endList(); out.push('<hr>'); continue }
    if ((m = /^>\s?(.*)$/.exec(t))) {   // a quote
      endPara(); endList(); const q = [m[1]];
      while (i + 1 < lines.length && (m = /^>\s?(.*)$/.exec(lines[i + 1].trim()))) { q.push(m[1]); i++ }
      out.push(`<blockquote dir="auto">${q.map(inline).join('<br>')}</blockquote>`); continue }
    if ((m = /^(#{1,4})\s+(.+)$/.exec(t))) { endPara(); endList(); out.push(`<h4 class="${m[1].length < 3 ? 'big' : ''}" dir="auto">${inline(m[2])}</h4>`); continue }
    if ((m = /^([A-Z][A-Z0-9 &/'-]{2,32})(\s*\([^)]*\))?:?$/.exec(t))) {   // RULES, GOAL, FINAL RESULT (nothing else)
      endPara(); endList(); const words = m[1].trim(); out.push(`<h4>${esc(words[0] + words.slice(1).toLowerCase())}${m[2] ? `<span> ${esc(m[2].trim())}</span>` : ''}</h4>`); continue }
    if ((m = /^(\d{1,3})[.)]\s+(.+)$/.exec(t))) { item('ol', m[2], +m[1]); continue }
    if ((m = /^[-•*–]\s+(.+)$/.exec(t))) {
      if (list && list.type === 'ol' && indented && !list.gap) { list.items.at(-1).subs.push(m[1]); continue }
      item('ul', m[1]); continue }
    if (list && !list.gap && (indented || short(t))) { list.items.at(-1).subs.push(t); continue }   // a detail of the item above
    endList(); para.push(t);
  }
  endPara(); endList();
  return `<div class="rt">${out.join('')}</div>`;
}
const dayOf = t => { const d = new Date(t * 1000), now = new Date(), days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  return days === 0 ? 'Today' : days === 1 ? 'Yesterday' : d.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'long'}) };
const openMsgs = new Set();   // long messages of the user that were opened in full
const seenMsgs = new Set();   // messages already shown once: only a new one fades in
let firstPaint = true;
function renderChat(){
  const msgs = chat.messages;
  $('#empty').hidden = msgs.length > 0;
  const waiting = state?.agent.status === 'waiting' ? state.agent.waiting : null;
  const lastAsk = [...msgs].reverse().find(m => m.kind === 'ask');
  // Every message of the agent: what it is, when, then the text.
  const isNew = m => !firstPaint && !seenMsgs.has(m.id) ? ' fresh' : '';
  const copy = m => `<button class="mini" data-copy="${esc(m.id)}" title="Copy this text">Copy</button>`;
  const entry = (m, kind, title, body, meta = '', tools = '') => `<article class="msg agent ${kind}${isNew(m)}"><header class="mh"><h3 class="mt">${title}</h3><span class="mm">${tools}${meta}<time>${timeOf(m.time)}</time></span></header>${body}</article>`;
  let day = '';
  const html = msgs.map(m => {
    const d = dayOf(m.time), sep = d !== day ? `<div class="msg day"><span>${esc(d)}</span></div>` : ''; day = d;
    return sep + one(m);
  }).join('');
  function one(m){
    if (m.role === 'user') {
      const long = m.text.length > 700 || m.text.split('\n').length > 12, open = openMsgs.has(m.id);
      return `<div class="msg user${isNew(m)}"><div class="bubble${long && !open ? ' clipped' : ''}">${rich(m.text)}</div>${long ? `<button class="more" data-more="${esc(m.id)}">${open ? 'Show less' : 'Show all'}</button>` : ''}<time>${timeOf(m.time)}${m.source === 'telegram' ? ', from Telegram' : ''}</time></div>`;
    }
    if (m.kind === 'plan') {
      const row = (k, v) => `<div class="rt-row"><span class="rt-k">${k}</span><span>${v}</span></div>`;
      const steps = (m.goals || []).map(g => { const w = /^(.*?)\s*\(done when: (.*)\)$/s.exec(g); return `<li>${w ? `${inline(w[1])}<div class="rt-sub quiet">Done when ${inline(w[2])}</div>` : inline(g)}</li>` }).join('');
      return entry(m, 'plan', esc(m.text === 'Plan' ? 'Plan' : m.text), `<div class="rt">${m.deliverable ? row('You get', inline(m.deliverable)) : ''}${(m.assumptions || []).length ? row('I decided', m.assumptions.map(inline).join(' ')) : ''}${(m.knowhow || []).length ? row('Working by', m.knowhow.map(esc).join('; ')) : ''}${briefHtml(m.brief)}<ol class="rt-list roomy">${steps}</ol></div>`);
    }
    if (m.kind === 'ask') {
      const live = waiting && m === lastAsk, aid = esc(m.ask_id ?? '');
      const say = {code: 'I need a code', question: 'Before I start', stuck: 'I need your help', approval: 'May I?'}[m.ask] || 'I need you for this';
      const hint = !live ? '' : m.ask === 'code' ? '<p class="hint">Type it in the box on the screen, or send it to the Telegram bot.</p>'
        : m.ask === 'question' ? `<p class="hint">Answer in the box on the screen, or:</p><div class="row"><button class="solid" data-reply="continue" data-ask="${aid}">Start anyway</button><button class="ghost" data-reply="deny" data-ask="${aid}">Stop the task</button></div>`
        : m.ask === 'stuck' ? `<p class="hint">Tell me what to do in the box on the screen, or:</p><div class="row"><button class="solid" data-reply="continue" data-ask="${aid}">Try again</button><button class="ghost" data-reply="deny" data-ask="${aid}">Stop the task</button></div>`
        : m.ask === 'approval' ? `<div class="row"><button class="solid" data-reply="approve" data-ask="${aid}">Approve</button><button class="ghost" data-reply="deny" data-ask="${aid}">Deny</button></div>`
        : `<div class="row"><button class="solid" data-reply="continue" data-ask="${aid}">I'm done</button></div>`;
      return entry(m, 'ask', say, rich(m.text) + hint);
    }
    if (m.kind === 'status') return `<div class="msg agent status${isNew(m)}"><span>${inline(m.text)}</span><time>${timeOf(m.time)}</time></div>`;
    if (m.kind === 'error') { const failed = /^I could not finish:\s*/.exec(m.text); return entry(m, 'error', failed ? 'Could not finish' : 'Problem', rich(failed ? m.text.slice(failed[0].length) : m.text)) }
    const last = m === msgs[msgs.length - 1] && !(state && ['running', 'waiting'].includes(state.agent.status));
    // Under the newest result or answer: what you can do with it, in one press.
    const next = extra => last ? `<div class="row next">${extra}<button class="ghost" data-change>Ask for a change</button></div>` : '';
    if (m.kind === 'result') return entry(m, 'result', 'Result', rich(m.text) + next(''), m.steps ? `<span>${m.steps} actions</span>` : '', copy(m));
    // An answer given in the chat without touching the desktop: if work was meant, one press sends it there.
    const redo = m.answered ? next(`<button class="ghost" data-run="${esc(m.answered)}">Do this on the desktop instead</button>`) : '';
    const found = /^What I had found so far:\s*\n/.exec(m.text);
    return entry(m, 'text', m.answered ? 'Answer' : found ? 'Found so far' : 'Update', rich(found ? m.text.slice(found[0].length) : m.text) + redo, '', copy(m));
  }
  const thread = $('#thread');
  const atBottom = thread.scrollTop + thread.clientHeight >= thread.scrollHeight - 60;
  thread.querySelectorAll('.msg').forEach(n => n.remove());
  thread.insertAdjacentHTML('beforeend', html);
  const grew = msgs.some(m => !seenMsgs.has(m.id)); msgs.forEach(m => seenMsgs.add(m.id)); firstPaint = false;
  const live = $('#live'); if (live) thread.appendChild(live);   // what it is doing right now stays last
  if (grew && !atBottom && !firstChat) $('#jump').hidden = false;   // something new arrived while you were reading further up
  if (!msgs.length) thread.scrollTop = 0;  // the start: read it from the top
  else if (atBottom || firstChat) { thread.scrollTop = thread.scrollHeight; if (innerWidth <= 720) window.scrollTo(0, document.body.scrollHeight) }
  firstChat = false;
  renderTelegram(); renderMemory(); renderSkills();
  const kh = JSON.stringify(chat.knowhow || []); if (kh !== renderChat.kh) { renderChat.kh = kh; renderKnowhow() }
}

let loading = 0, offline = false, turnIdle = 'Try again';
async function refresh(){
  const id = sel; if (loading === id || document.hidden) return;
  loading = id;
  try {
    const s = await api(wpath('state', id)); if (id !== sel) return;
    state = s; showMode();
    if (offline) { offline = false; const im = liveImg(); if (im) { delete im.dataset.live; im.removeAttribute('src') } stream() }  // back again: restart the picture
    const live = s.agent.status === 'running' || s.agent.status === 'waiting';
    if (live && Date.now() - costTimer > 4000) { costTimer = Date.now(); api(wpath('usage', id)).then(u => { if (id === sel) runCost = u.run ? (u.run.cost_usd || 0) : null }).catch(() => {}) }
    if (!live && s.agent.status === 'idle') runCost = null;
    render();
    if (s.chat_rev !== chatRev) { const c = await api(wpath('chat', id)); if (id !== sel) return; chatRev = s.chat_rev; chat = c; renderChat() }
  } catch (e) {
    if (id !== sel) return;
    $('#mode').dataset.state = 'offline'; $('#mode-text').textContent = 'Not connected'; $('#mode-meta').textContent = '';
    offline = true;
    // Say what is wrong and how to look, instead of an endless empty screen.
    $('#screen-offline').hidden = false;
    $('#screen-offline').innerHTML = `<p>Screenpilot is not answering.</p><p class="sub">It reconnects by itself when it is back. If this stays, run <code>./run.sh</code> again, or see why with <code>docker compose logs --tail 80</code>.</p>`;
  } finally { if (loading === id) loading = 0 }
}

/* ---------- composer ---------- */
function setPlanner(p){
  planner = p; localStorage.setItem('planner', p); localStorage.setItem('planner-v7', '1');
  $$('.planner button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.planner === p)));
  showMode();
}
// What the chosen mode uses right now and where the screenshots go (the server knows what is installed and which key is set).
const MODE_FALLBACK = {private: 'Everything stays on this Mac.', smart: 'A cloud model decides from a text description; screenshots stay on this Mac.', fast: 'Screenshots are sent to the cloud model.'};
function showMode(){
  const m = state?.modes?.[planner];
  $('#privacy').textContent = m ? m.privacy + (m.hint ? ' ' + m.hint + '.' : '') : MODE_FALLBACK[planner] || '';
  $('#privacy').classList.toggle('leaves', m ? m.leaves : planner === 'fast');
}
$$('.planner button').forEach(b => b.addEventListener('click', () => setPlanner(b.dataset.planner)));
const input = $('#input');
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, Math.max(160, innerHeight * 0.45)) + 'px' });
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit() } });
$('#composer').addEventListener('submit', e => {
  e.preventDefault();
  const text = input.value.trim(); if (!text || sending) return;
  sending = true; $('#send').disabled = true;
  // Shown at once: reading the message (is it about the last result, or new work?) can take a few seconds.
  $('#empty').hidden = true;
  $('#thread').insertAdjacentHTML('beforeend', `<div class="msg user pending"><div class="bubble">${rich(text)}</div></div><div class="msg live pending"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span>Reading your message</span></div>`);
  $('#thread').scrollTop = $('#thread').scrollHeight;
  act(async () => {
    const body = {text, planner}; if (workers.length > 1 && target === 'any') body.any = true;
    const r = await api(wpath('chat'), body);
    input.value = ''; input.style.height = 'auto';
    if (r.taken_as) { if (r.worker !== sel) select(r.worker) }  // an answer or a correction for the task that is running
    else if (r.worker && r.worker !== sel) { select(r.worker); toast(`Worker ${r.worker} is on it`) }
    else if (!r.worker) toast('Every worker is busy. The first one that is free takes it.');
    try { if (window.Notification && Notification.permission === 'default') Notification.requestPermission() } catch {}
    await refresh();
  }).finally(() => { sending = false; $('#send').disabled = false; $$('#thread .pending').forEach(n => n.remove()); renderChat() });
});
/* ---------- examples: long jobs, written the way that works (rules, goal, how, the exact result) ---------- */
/* What the agent decided to look for, before it searches (the plan's brief). */
const BRIEF = [['looking_for', 'Looking for'], ['signs', 'A match'], ['reject', 'Rejected'], ['where', 'Where']];
function briefHtml(brief) {
  const rows = BRIEF.filter(([k]) => brief && brief[k] && brief[k].length)
    .map(([k, label]) => `<div class="rt-row"><span class="rt-k">${label}</span><span>${Array.isArray(brief[k]) ? brief[k].map(inline).join('; ') : inline(brief[k])}</span></div>`);
  return rows.length ? `<div class="plan-brief"><p>Before I search, this is what I'm going by. Tell me here if any of it is off.</p>${rows.join('')}</div>` : '';
}
const RULES = `RULES
- Don't ask me anything. Decide yourself and keep working.
- Only stop for security (a login, a code, a captcha, a payment, personal data) and tell me what is on the screen.
- If something fails, skip it and move on.`;
const EXAMPLES = [
  {title: 'Scout creative photographers in a city',
   about: 'Instagram. Decides first who qualifies, searches for photographers directly, proves their brand work on their own profiles, and returns a shortlist of 5.',
   prompt: `${RULES}
- Don't follow, like, comment on or message anyone.
- Use only Instagram. Open profiles by typing instagram.com/<handle> in the address bar.

GOAL
Find 5 creative, cool Miami-based photographers with under 100k followers who have shot for real brands or magazines.

WHO WE WANT (hold every account against this before spending time on it)
- A photographer's own account: the bio says photographer, and the grid is their own work.
- Under 100,000 followers. The count is at the top of the profile; over 100k, leave at once.
- A look of their own: editorial, fashion, campaign or art-directed work with a recognisable style in color, light or framing.
- Not: wedding, event, real estate, studio portrait, stock-looking or amateur work, "DM for collab" accounts.
- Not photographers: model agencies, models, brands, stylists, magazines, repost and hub pages. Never keep these and never start from them.

NOTEBOOK (keep exactly these notes, update them as you go)
- TO VISIT: photographers still to open
- VISITED: photographers already opened, each with one word why not (never open one twice)
- KEPT: one note per photographer you keep: @handle, followers, brands, why

HOW
1. Search for photographers themselves. Use Instagram search with two or three words: "miami fashion photographer", "miami editorial photographer", "miami film photographer". Scroll the results and add every photographer account to TO VISIT before opening any. Skip names that say wedding, events, portraits, studio, models or mgmt. Use each search once.
2. Open each one. Read the follower count and the bio, look at the first rows of the grid. Anything from the Not lists, or over 100k: note it in VISITED and move on at once. Don't open posts of someone you are rejecting.
3. For the ones that pass, prove the brand work on their own profile: open 3 or 4 posts that look like campaigns or editorials and read the caption and the tags. You need at least two real brands or magazines named or tagged as the client ("for @brand", a post shared with the brand, a magazine credit). Friends and models don't count.
4. Check Miami: it is in the bio, or at least 3 recent posts are tagged in Miami.
5. When the searches show nothing new, don't search again. Get leads from the photographers themselves, in this order:
   a. On the profile of every photographer who passed step 2 (kept or nearly), open the similar accounts (the small button next to Follow) and add the photographers it suggests.
   b. Add the photographers they tag, credit or assist in their posts.
   c. Open a tag page by address, instagram.com/explore/tags/miamifashionphotographer (also miamieditorial, miamiphotographer), open posts that look editorial and add the accounts that posted them.
   Open a brand, magazine or agency only to check a credit, never to browse it.
6. Stop when you have 5 in KEPT. Only photographer accounts count as examined. If you have examined 40 and have fewer than 5, stop and report those.

FINAL RESULT (nothing else)
1. Name: instagram.com/handle (followers)
   Brands: Brand A, Brand B
   Why: one sentence on what makes the work stand out
If fewer than 5: how many photographers you examined.`},
  {title: 'Compare products across shops, into a spreadsheet',
   about: 'Three shops, eight products, every number checked on the product page. Ends with a sorted spreadsheet in Documents.',
   prompt: `${RULES}
- Don't buy anything, don't add to a cart, don't sign in.

GOAL
Compare the 8 best carry-on suitcases under $250 that fit 55 x 40 x 20 cm, so I can pick one.

NOTEBOOK
- FOUND: one note per suitcase: name, shop, price, size, weight, rating, number of reviews, link

HOW
1. Search three shops (amazon.com, a luggage brand's own shop, and one large department store). Sort by rating or best sellers.
2. Open each promising product page and read the numbers there, not from the search results. Skip anything with fewer than 200 reviews or a rating under 4.3.
3. Note each one in FOUND as soon as you have its numbers. Stop at 8.
4. Open the spreadsheet app and make a table: Name, Shop, Price, Size, Weight, Rating, Reviews, Link. One row per suitcase, sorted by rating, best first.
5. Save it as ~/Documents/carry-ons.ods.

FINAL RESULT (nothing else)
The three you would pick, each with one sentence why, and the path of the file.`},
  {title: 'A pricing brief on five competitors',
   about: 'Reads each pricing page, lines the plans up side by side and writes a one-page document with what stands out.',
   prompt: `${RULES}
- Use only each company's own website. Don't sign up for anything or start a trial.

GOAL
A one-page pricing brief on five project-management tools: Asana, Trello, Monday, ClickUp and Notion.

NOTEBOOK
- PRICING: one note per company: each plan's name, price per user per month (billed yearly), and the two limits that matter most on the free plan

HOW
1. Open each company's pricing page (type its address). If prices depend on a monthly/yearly switch, read the yearly ones.
2. Note the plans in PRICING before moving to the next company.
3. Open the documents app and write the brief: a title, one table (company, free plan limits, cheapest paid plan, mid plan, top listed plan), then three short observations about what the table shows.
4. Save it as ~/Documents/pricing-brief.odt.

FINAL RESULT (nothing else)
The three observations, and the path of the file. Say which numbers you could not find.`},
  {title: 'Plan a trip, day by day',
   about: 'Flights, three hotels and a four-day plan with opening hours checked, in one document. Books nothing.',
   prompt: `${RULES}
- Don't book or reserve anything, don't enter any personal details.

GOAL
Plan 4 days in Lisbon for two people, flying from Tel Aviv, Thursday to Monday in the second half of April.

NOTEBOOK
- FLIGHTS: three options: airline, times, stops, price for two
- HOTELS: three options: name, area, price per night, rating, link
- DAYS: one note per day

HOW
1. On Google Flights find three round-trip options: the cheapest, the fastest, and the best mix. Note them.
2. On Booking.com find three hotels in Baixa, Chiado or Alfama under 150 euros a night with a rating of 8.5 or more and free cancellation. Note them.
3. Plan four days: two or three sights a day that are near each other, with a place for lunch nearby. Check each sight's opening days and hours on its own website and note anything closed on the day you planned it.
4. Write it all in the documents app: flights, hotels, then day by day with times. Save it as ~/Documents/lisbon.odt.

FINAL RESULT (nothing else)
Your pick of flight and hotel with one sentence each, the total for flights plus four nights, and the path of the file.`},
  {title: 'Brief me on a topic, from sources',
   about: 'Reads six recent sources, keeps what each one claims with its date and link, and writes what is agreed and what is disputed.',
   prompt: `${RULES}
- Read the pages themselves, not search-result snippets. Skip anything behind a paywall.

GOAL
Brief me on where solid-state batteries for cars stand right now: who is closest to selling them, and what is still unsolved.

NOTEBOOK
- SOURCES: one note per source: who published it, the date, the link, and its two or three main claims with numbers

HOW
1. Find six sources from the last six months: two from car or battery makers themselves, two from news outlets, one technical or academic, one sceptical.
2. Open each and read it to the end. Note it in SOURCES before opening the next.
3. Write the brief in the documents app: what the sources agree on, where they disagree (name who says what), what none of them answers, and the list of sources with dates and links.
4. Save it as ~/Documents/solid-state-brief.odt.

FINAL RESULT (nothing else)
Five sentences: the state of things, who is closest, the main open problem, the biggest disagreement, and how much you trust the picture. Then the path of the file.`},
];
function useExample(text){
  input.value = text; input.dispatchEvent(new Event('input')); input.scrollTop = 0; input.focus(); input.setSelectionRange(0, 0);
}
$('#examples').innerHTML = EXAMPLES.map((e, i) => `<button class="example" data-example="${i}"><span class="example-title">${esc(e.title)}</span><span class="example-about">${esc(e.about)}</span></button>`).join('');
$('#examples').addEventListener('click', e => { const b = e.target.closest('[data-example]'); if (b) useExample(EXAMPLES[+b.dataset.example].prompt) });
$$('.idea').forEach(b => b.addEventListener('click', () => useExample(b.textContent)));
$('#ctl-stop').addEventListener('click', () => act(async () => { await api(wpath('stop'), {}); toast(workers.length > 1 ? `Worker ${sel} stopped` : 'Stopped') }));
// Reset needs a second click within a few seconds, so it is never triggered by accident.
let resetArmed = 0;
$('#ctl-reset').addEventListener('click', () => {
  if (Date.now() - resetArmed > 6000) {
    resetArmed = Date.now(); $('#ctl-reset').classList.add('confirm'); $('#ctl-reset-text').textContent = 'Click again to reset';
    toast('Click Reset again to close every app and start the browser fresh');
    setTimeout(() => { if (resetArmed && Date.now() - resetArmed >= 5900) { $('#ctl-reset').classList.remove('confirm'); $('#ctl-reset-text').textContent = 'Reset desktop' } }, 6000);
    return;
  }
  resetArmed = 0; $('#ctl-reset').classList.remove('confirm'); $('#ctl-reset-text').textContent = 'Resetting…';
  $('#ctl-reset').disabled = true;
  act(async () => { await api(wpath('reset'), {}); chatRev = -1; toast('Desktop reset: apps closed, browser on a fresh page'); await refresh() })
    .finally(() => { $('#ctl-reset').disabled = false; $('#ctl-reset-text').textContent = 'Reset desktop' });
});
// Show what the local eyes see: numbered text and controls over the live screen.
let eyesTimer = null;
$('#ctl-eyes').addEventListener('click', () => {
  const on = $('#ctl-eyes').getAttribute('aria-pressed') !== 'true';
  $('#ctl-eyes').setAttribute('aria-pressed', String(on)); $('#marks').hidden = !on; clearInterval(eyesTimer);
  if (!on) return;
  loadMarks(); eyesTimer = setInterval(loadMarks, 2500);
});
function loadMarks(){ const id = sel, i = new Image(); i.onload = () => { if (!$('#marks').hidden && id === sel) $('#marks').src = i.src }; i.src = `/api/w/${id}/marks?t=${Date.now()}` }
$('#thread').addEventListener('click', e => {
  const r = e.target.dataset.reply; if (r) return reply(r, e.target.dataset.ask);
  const cp = e.target.dataset.copy;
  if (cp) { const m = chat.messages.find(x => x.id === cp); if (m) copyText(m.text).then(() => { e.target.textContent = 'Copied'; e.target.classList.add('done'); setTimeout(() => { e.target.textContent = 'Copy'; e.target.classList.remove('done') }, 1600) }); return }
  if (e.target.dataset.change !== undefined) { input.focus(); input.placeholder = 'Say what to change: shorter, a table, another language, two more…'; return }
  const more = e.target.dataset.more;
  if (more) { openMsgs.has(more) ? openMsgs.delete(more) : openMsgs.add(more); return renderChat() }
  const run = e.target.dataset.run;
  if (run) { e.target.disabled = true; act(async () => { await api(wpath('chat'), {text: run, planner, queue: true}); await refresh() }) }
});
// Every answer names the question it is for, and only one answer is sent at a time: a double click or an old
// button can never answer the next question.
let replying = false;
function reply(answer, ask){
  if (replying) return;
  ask = ask || state?.agent?.waiting?.id;
  replying = true; $$('[data-reply], #turn-approve, #turn-deny, #turn-continue, #turn-stop, #turn-help-send').forEach(b => b.disabled = true);
  act(async () => {
    try { const r = await api(wpath('agent/reply'), {answer, ask}); if (r && r.stale) toast(r.message) ; setDriving(false); await refresh() }
    finally { replying = false; $$('[data-reply], #turn-approve, #turn-deny, #turn-continue, #turn-stop, #turn-help-send').forEach(b => b.disabled = false) }
  })
}
$('#turn-continue').addEventListener('click', () => reply('continue'));
$('#turn-code-form').addEventListener('submit', e => { e.preventDefault(); const c = $('#turn-code').value.trim(); if (c) reply(c) });
$('#turn-no-code').addEventListener('click', () => reply('deny'));
$('#turn-help-form').addEventListener('submit', e => { e.preventDefault(); reply($('#turn-help').value.trim() || 'continue') });
$('#turn-help').addEventListener('input', () => { $('#turn-help-send').textContent = $('#turn-help').value.trim() ? 'Send' : turnIdle });
$('#turn-stop').addEventListener('click', () => reply('deny'));
$('#turn-approve').addEventListener('click', () => reply('approve'));
$('#turn-deny').addEventListener('click', () => reply('deny'));

/* ---------- mode popover ---------- */
$('#mode').addEventListener('click', e => { e.stopPropagation(); const p = $('#mode-pop'); p.hidden = !p.hidden; $('#mode').setAttribute('aria-expanded', String(!p.hidden)); if (!p.hidden) loadCost() });
document.addEventListener('click', e => { if (!$('#mode-pop').contains(e.target)) { $('#mode-pop').hidden = true; $('#mode').setAttribute('aria-expanded', 'false') } });
async function loadCost(){ try { const u = await api(wpath('usage')); const r = u.run; $('#pop-cost').textContent = r ? `$${(r.cost_usd || 0).toFixed(3)}` : '–'; showUsage(u) } catch {} }

/* ---------- running token count: everything since the user last reset it; tasks and restarts do not reset it ---------- */
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n || 0);
function showUsage(u){
  const s = u && u.session; if (!s) return;
  const p = s.by_provider || {}, loc = p.local || {}, cloudTok = (p.anthropic?.total_tokens || 0) + (p.openai?.total_tokens || 0);
  const cost = s.cost_usd || 0;
  $('#usage').textContent = `${fmtTok(s.total_tokens)} tokens` + (cost ? ` · $${cost.toFixed(2)}` : '');
  $('#ses-since').textContent = 'Total since ' + new Date(s.since * 1000).toLocaleString([], {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  $('#ses-total').textContent = (s.total_tokens || 0).toLocaleString();
  $('#ses-io').textContent = `${fmtTok(s.input_tokens)} / ${fmtTok(s.output_tokens)}` + (s.cached_tokens ? ` · ${fmtTok(s.cached_tokens)} cached` : '');
  $('#ses-local').textContent = `${fmtTok(loc.total_tokens)} (free)`;
  $('#ses-cloud').textContent = fmtTok(cloudTok);
  $('#ses-calls').textContent = String(s.calls || 0);
  $('#ses-cost').textContent = `$${cost.toFixed(3)}`;
}
async function pollUsage(){ if (!document.hidden) try { showUsage(await api(wpath('usage'))) } catch {} }
setInterval(pollUsage, 3000); pollUsage();
$('#usage').addEventListener('click', e => { e.stopPropagation(); $('#mode').click() });
$('#usage-reset').addEventListener('click', async e => { e.stopPropagation(); try { showUsage(await api('/api/usage/reset', {})) } catch (err) { toast(err.message) } });

/* ---------- take over: you drive the same screen ---------- */
function setDriving(on){
  driving = on; $('#takeover').setAttribute('aria-pressed', String(on)); $('#takeover').textContent = on ? 'Stop driving' : 'Take over';
  $('#screen').classList.toggle('driving', on); if (on) $('#screen').focus();
  if (on) scrollToSelected('instant');
  if (state) render();
}
$('#takeover').addEventListener('click', e => { e.stopPropagation(); setDriving(!driving) });
let inputChain = Promise.resolve();
const send = a => { const path = wpath('action'); inputChain = inputChain.then(() => api(path, a)).catch(e => toast(e.message)) };
function point(e){
  // The picture keeps 16:10 inside its slide; map the pointer onto the 1280×800 desktop.
  const r = liveImg().getBoundingClientRect(), scale = Math.min(r.width / W, r.height / H), ox = (r.width - W * scale) / 2, oy = (r.height - H * scale) / 2;
  return {x: Math.max(0, Math.min(W-1, Math.round((e.clientX - r.left - ox) / scale))), y: Math.max(0, Math.min(H-1, Math.round((e.clientY - r.top - oy) / scale)))};
}
deck.addEventListener('click', e => { if (!driving || e.target !== liveImg()) return; const p = point(e); ripple(p.x, p.y); send({kind:'click', ...p, label:'Click'}) });
deck.addEventListener('dblclick', e => { if (!driving || e.target !== liveImg()) return; const p = point(e); send({kind:'double_click', ...p, label:'Double-click'}) });
let wheel = 0, wheelTimer;
$('#screen').addEventListener('wheel', e => { if (!driving) return; e.preventDefault(); wheel += Math.sign(e.deltaY); clearTimeout(wheelTimer); wheelTimer = setTimeout(() => { if (wheel) send({kind:'scroll', amount: Math.max(-10, Math.min(10, wheel))}); wheel = 0 }, 120) }, {passive:false});
const KEYS = {Enter:'Return', Backspace:'BackSpace', Tab:'Tab', Escape:'Escape', Delete:'Delete', ArrowUp:'Up', ArrowDown:'Down', ArrowLeft:'Left', ArrowRight:'Right', Home:'Home', End:'End', PageUp:'Page_Up', PageDown:'Page_Down', ' ':'space'};
let typed = '', typeTimer;
$('#screen').addEventListener('keydown', e => {
  if (!driving) return;
  const mod = e.ctrlKey || e.metaKey;
  if (!mod && e.key.length === 1) { e.preventDefault(); typed += e.key; clearTimeout(typeTimer); typeTimer = setTimeout(() => { send({kind:'type', text: typed, label:'Type'}); typed = '' }, 140); return }
  let key = KEYS[e.key] || (mod && /^[a-z0-9]$/i.test(e.key) ? e.key.toLowerCase() : null);
  if (!key) return;
  e.preventDefault();
  if (typed) { clearTimeout(typeTimer); send({kind:'type', text: typed, label:'Type'}); typed = '' }
  if (mod) key = 'ctrl+' + key; else if (e.shiftKey && key === 'Tab') key = 'shift+Tab';
  send({kind:'key', key, label:'Key'});
});

/* ---------- settings ---------- */
function openSheet(tab){ $('#sheet').hidden = false; if (tab) selectTab(tab); loadSettings() }
$('#open-settings').addEventListener('click', () => openSheet());
$('#close-settings').addEventListener('click', () => $('#sheet').hidden = true);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !driving) { $('#sheet').hidden = true; $('#mode-pop').hidden = true } });
function selectTab(name){
  $$('.tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === name))); $$('.tab').forEach(p => p.hidden = p.dataset.panel !== name);
}
$$('.tabs button').forEach(b => b.addEventListener('click', () => selectTab(b.dataset.tab)));

async function loadSettings(){
  await act(async () => {
    [settings, room] = await Promise.all([api('settings'), api('room')]);
    $('#s-anthropic-status').textContent = settings.keys_configured.anthropic ? 'A key is set. Paste a new one to replace it.' : 'No key yet.';
    $('#s-openai-status').textContent = settings.keys_configured.openai ? 'A key is set. Paste a new one to replace it.' : 'No key yet.';
    $('#s-run-budget').value = settings.run_budget_usd || ''; $('#s-day-budget').value = settings.daily_budget_usd || ''; $('#s-max-steps').value = settings.max_steps || '';
    $('#s-mac-eyes').checked = settings.mac_eyes !== false; $('#s-uitars-model').value = settings.uitars_model || '';
    $('#s-holo-model').value = settings.holo_model || ''; $('#s-cloud').value = settings.cloud_provider || 'auto';
    const gm = $('#s-openai-model');  // a model set in .env that is not in the list is shown as it is
    if (![...gm.options].some(o => o.value === settings.openai_model)) gm.add(new Option(settings.openai_model, settings.openai_model));
    gm.value = settings.openai_model; $('#s-contract').checked = settings.plan_contract !== false; $('#s-thrifty').checked = !!settings.thrifty; $('#s-ask').checked = !!settings.ask_when_stuck;
    const m = state?.modes; $('#s-modes').textContent = m ? `Right now: Private → ${NAMES[m.private.planner]}; Smart & private → ${NAMES[m.smart.planner]}; Fastest → ${NAMES[m.fast.planner]}.` : '';
    $('#s-profile').value = room.profile; $('#s-phone').value = room.phone || ''; $('#s-approval').checked = room.require_approval; $('#s-ntfy').value = room.ntfy_topic;
    $('#s-mail').value = room.mail_address; $('#s-imap').value = room.imap_host; $('#s-mail-pass').value = '';
    $('#s-mail-status').textContent = room.mail_configured ? 'The mailbox is set up.' : 'Not set up yet. For Gmail, create an app password at myaccount.google.com/apppasswords.';
    renderLogins();
  });
}
function renderLogins(){
  $('#logins').innerHTML = room.logins.map(l => `<li><span>${esc(l.name)}<small>${esc([l.site, l.username].filter(Boolean).join(' · ') || 'no details')}${l.has_password ? '' : ' · no password'}</small></span><button data-del-login="${esc(l.name)}">Delete</button></li>`).join('') || '<li><span class="hint">No saved logins yet.</span></li>';
}
$('#save-models').addEventListener('click', () => act(async () => {
  const body = {run_budget_usd: +$('#s-run-budget').value || 0, daily_budget_usd: +$('#s-day-budget').value || 0, max_steps: +$('#s-max-steps').value || 0, mac_eyes: $('#s-mac-eyes').checked};
  if ($('#s-uitars-model').value.trim()) body.uitars_model = $('#s-uitars-model').value.trim();
  body.cloud_provider = $('#s-cloud').value; body.openai_model = $('#s-openai-model').value; body.plan_contract = $('#s-contract').checked; body.thrifty = $('#s-thrifty').checked; body.ask_when_stuck = $('#s-ask').checked;
  if ($('#s-holo-model').value.trim()) body.holo_model = $('#s-holo-model').value.trim();
  if ($('#s-anthropic-key').value) body.anthropic_key = $('#s-anthropic-key').value;
  if ($('#s-openai-key').value) body.openai_key = $('#s-openai-key').value;
  await api('settings', body); $('#s-anthropic-key').value = ''; $('#s-openai-key').value = ''; toast('Models saved'); loadSettings();
}));
$('#save-you').addEventListener('click', () => act(async () => { room = await api('room', {profile: $('#s-profile').value, phone: $('#s-phone').value, require_approval: $('#s-approval').checked, ntfy_topic: $('#s-ntfy').value}); toast('Saved') }));
$('#save-mail').addEventListener('click', () => act(async () => {
  const body = {mail_address: $('#s-mail').value, imap_host: $('#s-imap').value}; if ($('#s-mail-pass').value) body.mail_password = $('#s-mail-pass').value;
  room = await api('room', body); toast('Mailbox saved'); loadSettings();
}));
$('#test-mail').addEventListener('click', () => act(async () => { toast('Connecting to the mailbox…'); const r = await api('room/mail-test', {}); toast(`The mailbox works. ${r.count} recent message${r.count === 1 ? '' : 's'}.`) }));
$('#save-login').addEventListener('click', () => act(async () => {
  const body = {name: $('#l-name').value, site: $('#l-site').value, username: $('#l-user').value}; if ($('#l-pass').value) body.password = $('#l-pass').value;
  room = await api('room/login', body); ['#l-name','#l-site','#l-user','#l-pass'].forEach(i => $(i).value = ''); renderLogins(); toast('Login saved');
}));
$('#logins').addEventListener('click', e => { const n = e.target.dataset.delLogin; if (n) act(async () => { room = await api('room/login/delete', {name: n}); renderLogins(); toast('Login deleted') }) });

function renderTelegram(){
  const t = chat.telegram || {};
  $('#tg-off').hidden = !!t.connected; $('#tg-on').hidden = !t.connected;
  if (!t.connected) return;
  $('#tg-state').textContent = t.paired ? `Connected to @${t.bot}. Message it any task; replies and questions arrive there too.` : `Bot @${t.bot} is ready. In Telegram, open it and send this code:`;
  $('#tg-code').hidden = !t.pair_code; $('#tg-code').textContent = t.pair_code || '';
  $('#tg-error').textContent = t.error ? 'Telegram says: ' + t.error : '';
}
$('#tg-connect').addEventListener('click', () => act(async () => { chat.telegram = await api('telegram', {token: $('#tg-token').value}); $('#tg-token').value = ''; renderTelegram(); toast('Telegram connected') }));
$('#tg-disconnect').addEventListener('click', () => act(async () => { chat.telegram = await api('telegram', {disconnect: true}); renderTelegram(); toast('Telegram disconnected') }));

/* What the agent is doing right now, as the last line of the chat: three moving dots and the words. */
function renderLive(){
  const a = state?.agent, on = a && a.status === 'running', thread = $('#thread');
  let el = $('#live');
  if (!on) { if (el) el.remove(); return }
  const atBottom = thread.scrollTop + thread.clientHeight >= thread.scrollHeight - 60;
  if (!el) { thread.insertAdjacentHTML('beforeend', '<div id="live" class="live"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span id="live-text"></span><span id="live-meta"></span></div>'); el = $('#live') }
  const step = a.plan && a.plan.length > 1 ? `step ${Math.min(a.goal_index + 1, a.plan.length)} of ${a.plan.length}` : '';
  $('#live-text').textContent = a.message || 'Working';
  $('#live-meta').textContent = [step, a.started_at ? clock(Date.now() / 1000 - a.started_at) : ''].filter(Boolean).join(', ');
  if (atBottom) thread.scrollTop = thread.scrollHeight;
}
$('#thread').insertAdjacentHTML('afterend', '<button type="button" class="jump" id="jump" hidden>New messages</button>');
$('#jump').addEventListener('click', () => { const t = $('#thread'); t.scrollTo({top: t.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); $('#jump').hidden = true });
$('#thread').addEventListener('scroll', () => { const t = $('#thread'); if (t.scrollTop + t.clientHeight >= t.scrollHeight - 60) $('#jump').hidden = true }, {passive: true});
async function copyText(text){
  try { await navigator.clipboard.writeText(text) }
  catch { const ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove() }
}
function renderKnowhow(){
  $('#knowhow').innerHTML = (chat.knowhow || []).map(k => `<li><details><summary><span>${esc(k.name)}<small>${k.use === 'always' ? 'Always. ' : k.use === 'planning' ? 'For the plan. ' : ''}${esc(k.when)}</small></span></summary>${rich(k.text)}${k.builtin ? '' : `<button class="ghost" data-del-kh="${esc(k.id)}">Remove</button>`}</details><label class="switch"><input type="checkbox" data-kh="${esc(k.id)}" ${k.on ? 'checked' : ''} aria-label="Use ${esc(k.name)}"> On</label></li>`).join('');
}
$('#knowhow').addEventListener('change', e => { const id = e.target.dataset.kh; if (id) act(async () => { chat.knowhow = (await api('knowhow/switch', {id, on: e.target.checked})).knowhow }) });
$('#knowhow').addEventListener('click', e => { const id = e.target.dataset.delKh; if (id) act(async () => { chat.knowhow = (await api('knowhow/delete', {id})).knowhow; renderKnowhow() }) });
$('#kh-add').addEventListener('click', () => act(async () => {
  chat.knowhow = (await api('knowhow', {name: $('#kh-name').value, when: $('#kh-when').value, text: $('#kh-text').value})).knowhow;
  $('#kh-name').value = $('#kh-when').value = $('#kh-text').value = ''; renderKnowhow(); toast('Added');
}));
function renderSkills(){
  $('#skills').innerHTML = (chat.skills || []).map(k => `<li><span>${esc(k.task)}<small>${k.steps} steps · replayed ${k.uses} time${k.uses === 1 ? '' : 's'}</small></span><button data-del-skill="${esc(k.id)}">Forget</button></li>`).join('') || '<li><span class="hint">No saved skills yet. Finish a task and it appears here.</span></li>';
}
$('#skills').addEventListener('click', e => { const id = e.target.dataset.delSkill; if (id) act(async () => { chat.skills = (await api('skills/delete', {id})).skills; renderSkills() }) });

function renderMemory(){
  $('#memory').innerHTML = (chat.memory || []).slice().reverse().map(m => `<li><span>${esc(m.text)}<small>${m.source === 'user' ? 'Added by you' : 'Saved by the agent'}</small></span><button data-del-mem="${esc(m.id)}">Forget</button></li>`).join('') || '<li><span class="hint">Nothing remembered yet.</span></li>';
}
$('#m-add').addEventListener('click', () => act(async () => { const r = await api('memory', {text: $('#m-text').value}); chat.memory = r.memory; $('#m-text').value = ''; renderMemory() }));
$('#memory').addEventListener('click', e => { const id = e.target.dataset.delMem; if (id) act(async () => { chat.memory = (await api('memory/delete', {id})).memory; renderMemory() }) });
$('#clear-chat').addEventListener('click', () => act(async () => { await api(wpath('chat/clear'), {}); chatRev = -1; toast('Conversation cleared'); refresh() }));

/* ---------- logs: what happened, how long it took, what is still going on ---------- */
let logAfter = 0, logFor = null;
const logOpen = () => !$('#livelog').hidden;
// The chat pane shows the conversation or the live log.
function paneTab(name){
  $$('.pane-tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.pane === name)));
  $('#thread').hidden = name !== 'chat'; $('#livelog').hidden = name !== 'log';
  try { localStorage.setItem('pane', name) } catch {}
  if (name === 'log') { resetLog(); loadLog() }
}
$$('.pane-tabs button').forEach(b => b.addEventListener('click', () => paneTab(b.dataset.pane)));

/* ---------- the divider: drag it to give the chat or the screen more room ---------- */
const splitter = $('#splitter');
function chatWidth(w){
  w = Math.round(Math.max(340, Math.min(innerWidth - 400, w)));
  document.documentElement.style.setProperty('--chat-w', w + 'px'); return w;
}
function saveChatWidth(w){ try { localStorage.setItem('chat-w', String(w)) } catch {} }
try { const w = +localStorage.getItem('chat-w'); if (w) chatWidth(w) } catch {}
splitter.addEventListener('pointerdown', e => {
  e.preventDefault(); splitter.setPointerCapture(e.pointerId); splitter.classList.add('dragging'); document.body.classList.add('resizing');
  const move = ev => chatWidth(ev.clientX);
  const up = ev => { splitter.removeEventListener('pointermove', move); splitter.classList.remove('dragging'); document.body.classList.remove('resizing'); saveChatWidth(chatWidth(ev.clientX)) };
  splitter.addEventListener('pointermove', move); splitter.addEventListener('pointerup', up, {once: true}); splitter.addEventListener('pointercancel', up, {once: true});
});
splitter.addEventListener('keydown', e => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault(); saveChatWidth(chatWidth($('.chat-pane').getBoundingClientRect().width + (e.key === 'ArrowRight' ? 32 : -32)));
});
splitter.addEventListener('dblclick', () => { document.documentElement.style.removeProperty('--chat-w'); try { localStorage.removeItem('chat-w') } catch {} });
function resetLog(){ logAfter = 0; logFor = null; $('#log-lines').innerHTML = '' }
function logLine(e){
  const t = new Date(e.t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  const took = e.ms != null ? ` (${(e.ms / 1000).toFixed(1)} s)` : '';
  const extra = Object.entries(e).filter(([k]) => !['id','t','w','level','kind','msg','ms','trace'].includes(k)).map(([k, v]) => `${k}=${v}`).join(' ');
  return `<div class="${esc(e.level)}"><span class="t">${t}${e.w ? ' W' + e.w : ''} ${esc(e.kind)}</span> ${esc(e.msg)}${took}${extra ? ` <span class="t">[${esc(extra)}]</span>` : ''}` +
    (e.trace ? `<div class="trace">${esc(e.trace)}</div>` : '') + '</div>';
}
async function loadLog(){
  if (!logOpen()) return;
  const all = $('#log-all').checked, key = all ? 'all' : sel;
  if (logFor !== key) { resetLog(); logFor = key }
  try {
    const r = await api(`w/${sel}/logs?after=${logAfter}${all ? '&all=1' : ''}`);
    if (logFor !== key) return;
    $('#log-now').innerHTML = r.now.map(n => `<li class="${n.seconds > 30 ? 'slow' : ''}"><span>${n.w ? 'Worker ' + n.w + ' · ' : ''}${esc(n.msg)}</span><small>${clock(n.seconds)}</small></li>`).join('') || '<li><span class="hint">Nothing in progress.</span></li>';
    if (r.entries.length) {
      const box = $('#log-lines'), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      box.insertAdjacentHTML('beforeend', r.entries.map(logLine).join(''));
      while (box.children.length > 800) box.firstChild.remove();
      logAfter = r.entries[r.entries.length - 1].id;
      if (atEnd) box.scrollTop = box.scrollHeight;
    }
  } catch (e) { $('#log-now').innerHTML = `<li><span class="hint">${esc(e.message)}</span></li>` }
}
$('#log-all').addEventListener('change', () => { resetLog(); loadLog() });
$('#pop-log').addEventListener('click', e => { e.stopPropagation(); $('#mode-pop').hidden = true; paneTab('log') });

/* ---------- start ---------- */
(async () => {
  // Local first: screenshots stay on this Mac unless you pick a cloud model.
  const VALID = ['private', 'smart', 'fast'];
  // A choice saved by an older version becomes the mode closest to it.
  const mode = p => VALID.includes(p) ? p : !p ? null : p.endsWith('_text') ? 'smart' : p.endsWith('_direct') ? 'fast' : 'private';
  planner = mode(planner);
  if (!planner) {
    try { planner = mode((await api('settings')).planner) } catch {}
    planner = planner || 'private';
  }
  setPlanner(planner);
  syncDeck();
  await refreshWorkers();
  if (!slides.has(sel)) sel = 1;
  select(sel, 'instant'); stream(); renderRail();
  await refresh(); loadThumbs();
  setInterval(refresh, 600); setInterval(refreshWorkers, 1200); setInterval(loadThumbs, 2000); setInterval(loadLog, 1500);
})();
