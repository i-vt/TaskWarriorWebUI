const VIEWS = [
  { id:'pending',   label:'Pending',   icon:'◈', q:{status:'pending'},                 count:'pending' },
  { id:'active',    label:'Active',    icon:'▶', q:{vtag:'ACTIVE'},                     count:'active' },
  { id:'duesoon',   label:'Due Soon',  icon:'◷', q:{status:'pending', vtag:'DUE'},      count:'due_soon' },
  { id:'overdue',   label:'Overdue',   icon:'!', q:{vtag:'OVERDUE'},                    count:'overdue' },
  { id:'scheduled', label:'Scheduled', icon:'⧖', q:{status:'pending', vtag:'SCHEDULED'},count:'scheduled' },
  { id:'blocked',   label:'Blocked',   icon:'⊘', q:{vtag:'BLOCKED'},                    count:'blocked' },
  { id:'blocking',  label:'Blocking',  icon:'⊛', q:{vtag:'BLOCKING'},                   count:'blocking' },
  { id:'waiting',   label:'Waiting',   icon:'⏸', q:{status:'waiting'},                  count:'waiting' },
  { id:'recurring', label:'Recurring', icon:'↻', q:{status:'recurring'},               count:'recurring' },
  { id:'completed', label:'Completed', icon:'✓', q:{status:'completed'},               count:'completed' },
  { id:'deleted',   label:'Deleted',   icon:'✗', q:{status:'deleted'},                 count:'deleted' },
  { id:'all',       label:'All',       icon:'∗', q:{},                                 count:null },
  { id:'reports',   label:'Reports',   icon:'▤', reports:true,                         count:null },
];

let currentViewId = 'pending';
let currentProject = '';
let currentTag = '';
let rawFilter = '';
let sortKey = 'urgency';
let allTasks = [];
let searchQuery = '';
let meta = { priorities:['H','M','L'], udas:[], contexts:{list:[],active:null}, projects:[], tags:[], _stats:null };

let modalMode = 'add';   // add | log | edit
let editUuid = null;
let pendingDeps = [];    // [{uuid,label}]
let pendingAnnos = [];   // [string]

// ─── UTILS ──────────────────────────────────────────
function toast(msg, type='success') {
  const c = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`; el.textContent = msg;
  c.appendChild(el); setTimeout(() => el.remove(), 5200);
}
function esc(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}
function twToDate(s) {
  if (!s) return null;
  const m = s.replace(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/, '$1-$2-$3T$4:$5:$6Z');
  const d = new Date(m); return isNaN(d) ? null : d;
}
function fmtDate(s) {
  const d = twToDate(s); if (!d) return '—';
  return d.toLocaleDateString(undefined, { month:'short', day:'numeric', year:'2-digit' });
}
function fmtDateTime(s) {
  const d = twToDate(s); if (!d) return '—';
  const hasTime = !/T000000Z$/.test(s);
  const base = d.toLocaleDateString(undefined, { month:'short', day:'numeric', year:'numeric' });
  return hasTime ? base + ' ' + d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}) : base;
}
function relTime(s) {
  const d = twToDate(s); if (!d) return '';
  const diff = d - new Date(), day = 86400000;
  const days = Math.round(diff/day);
  if (Math.abs(diff) < day) {
    const h = Math.round(diff/3600000);
    if (Math.abs(h) < 1) return 'now';
    return h>0 ? `in ${h}h` : `${-h}h ago`;
  }
  return days>0 ? `in ${days}d` : `${-days}d ago`;
}
function isOverdue(s){ const d=twToDate(s); return !!d && d < new Date(); }
function isSoon(s){ const d=twToDate(s); if(!d) return false; const diff=d-new Date(); return diff>0 && diff<86400000*3; }
function localToTW(val){ if(!val) return ''; const d=new Date(val); if(isNaN(d)) return val; return d.toISOString().replace(/\.\d{3}Z$/,'Z'); }
function twToLocalInput(s){ const d=twToDate(s); if(!d) return ''; const p=n=>String(n).padStart(2,'0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; }

async function api(method, path, body) {
  const opts = { method, headers:{ 'Content-Type':'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) { if (r.status===401) { window.location.href='/login'; } const msg=(data&&data.error)?data.error:`Error ${r.status}`; toast(msg,'error'); throw new Error(msg); }
  return data;
}

// ─── VIEWS / SIDEBAR ────────────────────────────────
function renderViews(stats) {
  const list = document.getElementById('view-list'); list.innerHTML = '';
  VIEWS.forEach(v => {
    const el = document.createElement('div');
    el.className = 'nav-item' + (v.id===currentViewId && !currentProject && !currentTag ? ' active' : '');
    const cnt = (v.count && stats) ? `<span class="count">${stats[v.count] ?? 0}</span>` : '';
    el.innerHTML = `<span class="icon">${v.icon}</span> ${v.label} ${cnt}`;
    el.addEventListener('click', () => selectView(v.id));
    list.appendChild(el);
  });
}
function selectView(id) {
  currentViewId = id; currentProject=''; currentTag='';
  const v = VIEWS.find(x => x.id===id);
  document.getElementById('view-title').textContent = v.label;
  renderViews(meta._stats);
  if (v.reports) showReports(); else { hideReports(); loadTasks(); }
}
function buildTaskQuery() {
  const v = VIEWS.find(x => x.id===currentViewId) || VIEWS[0];
  const p = new URLSearchParams();
  if (!currentProject && !currentTag && v.q) {
    if (v.q.status) p.set('status', v.q.status);
    if (v.q.vtag) p.set('vtag', v.q.vtag);
  } else {
    const st = (currentViewId==='completed'||currentViewId==='deleted') ? currentViewId : 'pending';
    p.set('status', st);
  }
  if (currentProject) p.set('project', currentProject);
  if (currentTag) p.set('tag', currentTag);
  if (rawFilter) p.set('filter', rawFilter);
  p.set('sort', sortKey);
  return p.toString();
}

// ─── OVERVIEW ───────────────────────────────────────
async function loadOverview() {
  let o;
  try { o = await api('GET', '/api/overview'); } catch { return; }
  meta.priorities = (o.priorities && o.priorities.length) ? o.priorities : ['H','M','L'];
  meta.udas = o.udas || [];
  meta.contexts = o.contexts || { list:[], active:null };
  meta.projects = (o.projects||[]).map(x=>x.name);
  meta.tags = (o.tags||[]).map(x=>x.name);
  meta._stats = o.stats;

  const s = o.stats || {};
  document.getElementById('stat-pending').textContent = s.pending ?? '–';
  document.getElementById('stat-active').textContent = s.active ?? '–';
  document.getElementById('stat-overdue').textContent = s.overdue ?? '–';
  document.getElementById('stat-completed').textContent = s.completed ?? '–';
  renderViews(s);

  const pl = document.getElementById('project-list'); pl.innerHTML='';
  const dl = document.getElementById('project-suggestions'); dl.innerHTML='';
  (o.projects||[]).forEach(p => {
    dl.innerHTML += `<option value="${esc(p.name)}">`;
    const el = document.createElement('div');
    el.className = 'nav-item' + (currentProject===p.name ? ' active' : '');
    el.innerHTML = `<span class="icon">◇</span> ${esc(p.name)} <span class="count">${p.count}</span>`;
    el.addEventListener('click', () => {
      currentProject=p.name; currentTag=''; currentViewId='pending';
      document.getElementById('view-title').textContent = `Project · ${p.name}`;
      hideReports();
      document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));
      el.classList.add('active'); loadTasks();
    });
    pl.appendChild(el);
  });

  const tl = document.getElementById('tag-list'); tl.innerHTML='';
  const tdl = document.getElementById('tag-suggestions'); tdl.innerHTML='';
  (o.tags||[]).forEach(t => {
    tdl.innerHTML += `<option value="${esc(t.name)}">`;
    const el = document.createElement('div');
    el.className = 'tag-chip' + (currentTag===t.name ? ' active' : '');
    el.innerHTML = `${esc(t.name)} <span class="count">${t.count}</span>`;
    el.addEventListener('click', () => {
      currentTag = (currentTag===t.name ? '' : t.name); currentProject='';
      if (!currentTag) currentViewId='pending';
      document.getElementById('view-title').textContent = currentTag ? `Tag · +${t.name}` : 'Pending';
      hideReports();
      document.querySelectorAll('.tag-chip').forEach(c=>c.classList.toggle('active', c===el && currentTag));
      loadTasks();
    });
    tl.appendChild(el);
  });

  const cs = document.getElementById('context-select');
  cs.innerHTML = '<option value="none">— none —</option>';
  (meta.contexts.list||[]).forEach(c => {
    const op = document.createElement('option'); op.value=c.name;
    op.textContent = c.name + (c.filter?`  (${c.filter})`:''); cs.appendChild(op);
  });
  cs.value = meta.contexts.active || 'none';

  renderUdaFields();
}
// ─── TASK LIST ──────────────────────────────────────
async function loadTasks() {
  const rows = document.getElementById('task-rows');
  rows.innerHTML = `<div class="loading-row"><span class="spinner"></span> Loading…</div>`;
  try { allTasks = await api('GET', '/api/tasks?' + buildTaskQuery()); }
  catch { allTasks = []; }
  renderTasks();
}
function glyphsFor(t) {
  const g = [];
  if (t.recur || t.status==='recurring') g.push('<span class="g-rec" title="recurring">↻</span>');
  if (t.scheduled) g.push('<span class="g-sch" title="scheduled">⧖</span>');
  if ((t.depends||[]).length) g.push('<span class="g-blk" title="has dependencies">⊘</span>');
  if ((t.annotations||[]).length) g.push(`<span class="g-ann" title="${(t.annotations||[]).length} annotation(s)">❏</span>`);
  return g.length ? `<span class="glyphs">${g.join(' ')}</span>` : '';
}
function renderTasks() {
  const rows = document.getElementById('task-rows');
  let tasks = allTasks.slice();
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    tasks = tasks.filter(t =>
      (t.description||'').toLowerCase().includes(q) ||
      (t.project||'').toLowerCase().includes(q) ||
      (t.tags||[]).some(tag => tag.toLowerCase().includes(q)) ||
      (t.annotations||[]).some(a => (a.description||'').toLowerCase().includes(q)));
  }
  if (!tasks.length) { rows.innerHTML = `<div class="empty-state"><div class="big">◈</div><div class="msg">No tasks found</div></div>`; return; }
  rows.innerHTML = tasks.map(t => {
    const overdue=isOverdue(t.due), soon=isSoon(t.due), active=!!t.start;
    const urgency=parseFloat(t.urgency||0).toFixed(1); const uf=parseFloat(urgency);
    const urgHigh=uf>10, urgMed=uf>5&&!urgHigh; const barW=Math.min(60,Math.max(4,uf*3));
    const prio=t.priority||''; const done=(t.status==='completed'||t.status==='deleted');
    let cls='task-row'; if(done) cls+=' done-task'; else if(overdue) cls+=' overdue'; else if(active) cls+=' active-task';
    const tags=(t.tags||[]).map(tag=>`<span class="tag">${esc(tag)}</span>`).join('');
    const dueTxt = (done && t.end) ? fmtDate(t.end) : fmtDate(t.due);
    return `<div class="${cls}" data-uuid="${t.uuid}">
      <div class="task-check ${done?'ghost':''}" data-uuid="${t.uuid}" title="Mark done">✓</div>
      <div class="task-id">${t.id||''}</div>
      <div class="task-desc">${esc(t.description)}${glyphsFor(t)}<span class="tags">${tags}</span></div>
      <div class="task-project">${esc(t.project||'')}</div>
      <div class="task-priority ${prio||'none'}">${prio||'—'}</div>
      <div class="task-due ${overdue?'overdue':soon?'soon':''}">${dueTxt}</div>
      <div class="task-urgency">${urgency}<span class="bar ${urgHigh?'high':urgMed?'med':''}" style="width:${barW}px"></span></div>
    </div>`;
  }).join('');
  rows.querySelectorAll('.task-row').forEach(row => {
    row.addEventListener('click', e => { if (e.target.classList.contains('task-check')) return; openDetail(row.dataset.uuid); });
  });
  rows.querySelectorAll('.task-check:not(.ghost)').forEach(chk => {
    chk.addEventListener('click', async e => {
      e.stopPropagation();
      try { await api('POST', `/api/tasks/${chk.dataset.uuid}/done`); toast('Task completed ✓'); refresh(); }
      catch {}
    });
  });
}

// ─── DETAIL MODAL ───────────────────────────────────
let detailTask = null;
async function openDetail(uuid) {
  try { detailTask = await api('GET', `/api/tasks/${uuid}`); }
  catch { return; }
  renderDetail();
  document.getElementById('detail-modal').classList.add('open');
}
async function refreshDetail() {
  if (!detailTask) return;
  try { detailTask = await api('GET', `/api/tasks/${detailTask.uuid}`); renderDetail(); } catch {}
}
function badgesFor(t) {
  const b = [];
  if (isOverdue(t.due) && t.status==='pending') b.push('<span class="vbadge vb-over">OVERDUE</span>');
  if (t.start) b.push('<span class="vbadge vb-active">ACTIVE</span>');
  const blocked = (t.depends_detail||[]).some(d => d.status==='pending' || d.status==='waiting');
  if (blocked) b.push('<span class="vbadge vb-blocked">BLOCKED</span>');
  if (t.scheduled) b.push('<span class="vbadge vb-sched">SCHEDULED</span>');
  if (t.status==='waiting') b.push('<span class="vbadge vb-wait">WAITING</span>');
  if (t.recur || t.status==='recurring') b.push('<span class="vbadge vb-recur">RECURRING</span>');
  return b.join('');
}
function renderDetail() {
  const t = detailTask;
  const body = document.getElementById('detail-body');
  const overdue = isOverdue(t.due);

  const item = (k,v,extra='') => `<div class="detail-item ${extra}"><div class="detail-key">${k}</div><div class="detail-val">${v}</div></div>`;
  const dueVal = t.due ? `${fmtDateTime(t.due)} <span style="color:var(--text-muted)">(${relTime(t.due)})</span>` : '—';

  const depHtml = (t.depends_detail||[]).length
    ? (t.depends_detail||[]).map(d => `<span class="dep-link ${d.status!=='pending'&&d.status!=='waiting'?'done':''}" data-uuid="${d.uuid}">#${d.id||'?'} ${esc(d.description||'(unknown)')}</span> <span class="x" data-dep="${d.uuid}" title="remove dependency" style="cursor:pointer;color:var(--text-muted)">✕</span>`).join('<br>')
    : '<span style="color:var(--text-muted)">none</span>';

  const annoHtml = (t.annotations||[]).length
    ? (t.annotations||[]).map(a => `<div class="anno-item"><span class="when">${fmtDate(a.entry)}</span><span class="txt">${esc(a.description)}</span><span class="x" data-anno="${esc(a.description)}" title="remove">✕</span></div>`).join('')
    : '<span style="color:var(--text-muted)">none</span>';

  let udaHtml = '';
  (meta.udas||[]).forEach(u => { if (t[u.name]!==undefined && t[u.name]!=='') udaHtml += item(u.label, esc(t[u.name])); });

  body.innerHTML = `
    <div class="detail-badges">${badgesFor(t)||'<span style="color:var(--text-muted);font-size:11px">no flags</span>'}</div>
    <div class="detail-grid">
      ${item('Description', `<span style="font-size:15px;font-weight:500">${esc(t.description)}</span>`, 'detail-full')}
      ${item('Status', t.status||'—')}
      ${item('Priority', `<span class="task-priority ${t.priority||'none'}">${t.priority||'None'}</span>`)}
      ${item('Project', `<span style="color:var(--blue)">${esc(t.project||'None')}</span>`)}
      ${item('Urgency', parseFloat(t.urgency||0).toFixed(2))}
      ${item('Due', `<span class="${overdue?'task-due overdue':''}">${dueVal}</span>`)}
      ${item('Scheduled', fmtDateTime(t.scheduled))}
      ${item('Wait until', fmtDateTime(t.wait))}
      ${item('Until (expire)', fmtDateTime(t.until))}
      ${item('Recur', t.recur ? esc(t.recur) : '—')}
      ${item('Tags', (t.tags||[]).map(x=>`<span style="color:var(--purple)">+${esc(x)}</span>`).join(' ') || '—')}
      ${item('Created', fmtDateTime(t.entry))}
      ${item('Modified', fmtDateTime(t.modified))}
      ${t.start ? item('Started', fmtDateTime(t.start)) : ''}
      ${t.end ? item('Ended', fmtDateTime(t.end)) : ''}
      ${udaHtml}
      ${item('Depends on', depHtml, 'detail-full')}
      <div class="detail-item detail-full">
        <div class="detail-key">Annotations</div>
        <div class="detail-val">
          <div class="anno-add" style="margin-bottom:6px"><input type="text" class="form-input" id="detail-anno-input" placeholder="Add a note…"><button class="btn small" id="detail-anno-add">+ note</button></div>
          <div class="anno-list">${annoHtml}</div>
        </div>
      </div>
      ${item('UUID', `<span style="color:var(--text-muted);font-size:11px">${t.uuid}</span>`, 'detail-full')}
    </div>`;

  // dependency links + removal
  body.querySelectorAll('.dep-link').forEach(l => l.addEventListener('click', () => openDetail(l.dataset.uuid)));
  body.querySelectorAll('[data-dep]').forEach(x => x.addEventListener('click', async () => {
    try { await api('PATCH', `/api/tasks/${t.uuid}`, { depends_remove:[x.dataset.dep] }); toast('Dependency removed'); refreshDetail(); loadTasks(); } catch {}
  }));
  // annotation add / remove
  const aInput = document.getElementById('detail-anno-input');
  const addAnno = async () => {
    const txt = aInput.value.trim(); if (!txt) return;
    try { await api('POST', `/api/tasks/${t.uuid}/annotate`, { text:txt }); aInput.value=''; toast('Annotation added'); refreshDetail(); loadTasks(); } catch {}
  };
  document.getElementById('detail-anno-add').addEventListener('click', addAnno);
  aInput.addEventListener('keydown', e => { if (e.key==='Enter') addAnno(); });
  body.querySelectorAll('[data-anno]').forEach(x => x.addEventListener('click', async () => {
    try { await api('POST', `/api/tasks/${t.uuid}/denotate`, { text:x.dataset.anno }); toast('Annotation removed'); refreshDetail(); loadTasks(); } catch {}
  }));

  renderDetailActions();
}
function actBtn(label, cls, fn) { const b=document.createElement('button'); b.className='btn '+cls; b.textContent=label; b.onclick=fn; return b; }
function renderDetailActions() {
  const t = detailTask;
  const right = document.getElementById('detail-actions'); right.innerHTML='';
  const left = document.getElementById('detail-actions-left'); left.innerHTML='';

  left.appendChild(actBtn('✎ Edit','', () => { closeDetail(); openEdit(t); }));
  left.appendChild(actBtn('⧉ Duplicate','small', async () => { try { await api('POST', `/api/tasks/${t.uuid}/duplicate`); toast('Task duplicated'); closeDetail(); refresh(); } catch {} }));

  if (t.status==='pending') {
    if (!t.start) right.appendChild(actBtn('▶ Start','success', async () => { try { await api('POST', `/api/tasks/${t.uuid}/start`); toast('Task started'); closeDetail(); refresh(); } catch {} }));
    else right.appendChild(actBtn('⏹ Stop','', async () => { try { await api('POST', `/api/tasks/${t.uuid}/stop`); toast('Task stopped'); closeDetail(); refresh(); } catch {} }));
    right.appendChild(actBtn('✓ Mark Done','primary', async () => { try { await api('POST', `/api/tasks/${t.uuid}/done`); toast('Task completed ✓'); closeDetail(); refresh(); } catch {} }));
  }
  if (t.status!=='deleted') {
    right.appendChild(actBtn('✗ Delete','danger', async () => { if(!confirm('Delete this task?')) return; try { await api('DELETE', `/api/tasks/${t.uuid}`); toast('Task deleted','error'); closeDetail(); refresh(); } catch {} }));
  } else {
    right.appendChild(actBtn('⌦ Purge','danger', async () => { if(!confirm('Permanently purge this task? This cannot be undone.')) return; try { await api('POST', `/api/tasks/${t.uuid}/purge`); toast('Task purged','error'); closeDetail(); refresh(); } catch {} }));
  }
}
function closeDetail(){ document.getElementById('detail-modal').classList.remove('open'); }
// ─── UDA + PRIORITY FIELDS ──────────────────────────
function renderUdaFields() {
  const prio = document.getElementById('add-priority');
  const cur = prio.value;
  prio.innerHTML = '<option value="">None</option>' + meta.priorities.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  prio.value = cur;
  const wrap = document.getElementById('uda-fields');
  const section = document.getElementById('uda-section');
  if (!meta.udas.length) { section.classList.add('hidden'); wrap.innerHTML=''; return; }
  section.classList.remove('hidden');
  wrap.innerHTML = meta.udas.map(u => {
    let input;
    if (u.values && u.values.length) {
      input = `<select class="form-select" id="uda-${esc(u.name)}"><option value=""></option>${u.values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('')}</select>`;
    } else if (u.type==='numeric') {
      input = `<input type="number" step="any" class="form-input" id="uda-${esc(u.name)}">`;
    } else if (u.type==='date') {
      input = `<input type="datetime-local" class="form-input" id="uda-${esc(u.name)}">`;
    } else {
      input = `<input type="text" class="form-input" id="uda-${esc(u.name)}">`;
    }
    return `<div class="form-group"><label class="form-label">${esc(u.label)}</label>${input}</div>`;
  }).join('');
}
function collectUdas() {
  const out = {};
  meta.udas.forEach(u => {
    const el = document.getElementById(`uda-${u.name}`);
    if (!el) return;
    let v = el.value;
    if (u.type==='date' && v) v = localToTW(v);
    if (v!=='' && v!=null) out[u.name]=v;
  });
  return out;
}

// ─── ADD / EDIT / LOG MODAL ─────────────────────────
let depPickerTasks = [];
function applyModeVisibility() {
  const creating = (modalMode!=='edit');
  document.getElementById('add-mode').style.display = creating ? '' : 'none';
  const notLogRow = document.querySelector('.form-row.three.add-only');
  if (notLogRow) notLogRow.style.display = (modalMode==='log') ? 'none' : '';
  document.getElementById('depends-group').style.display = (modalMode==='add') ? '' : 'none';
  document.getElementById('anno-group').style.display    = (modalMode==='add') ? '' : 'none';
}
function setMode(m) {
  modalMode = m;
  document.querySelectorAll('#add-mode button').forEach(b => b.classList.toggle('active', b.dataset.mode===m));
  document.getElementById('add-submit').textContent = (m==='log') ? 'Log Task' : 'Add Task';
  applyModeVisibility();
}
function clearAddFields() {
  ['add-desc','add-project','add-tags','add-recur'].forEach(id => document.getElementById(id).value='');
  ['add-due','add-scheduled','add-wait','add-until'].forEach(id => document.getElementById(id).value='');
  document.getElementById('add-priority').value='';
  meta.udas.forEach(u => { const el=document.getElementById(`uda-${u.name}`); if (el) el.value=''; });
  pendingDeps=[]; pendingAnnos=[]; renderDepChips(); renderAnnoEditor();
}
async function openAdd() {
  editUuid=null;
  document.getElementById('add-title').textContent='New Task';
  setMode('add'); clearAddFields();
  try { depPickerTasks = await api('GET','/api/tasks?status=pending&sort=id'); } catch { depPickerTasks=[]; }
  fillDepSelect();
  document.getElementById('add-modal').classList.add('open');
  setTimeout(()=>document.getElementById('add-desc').focus(),50);
}
function openEdit(t) {
  editUuid = t.uuid; modalMode='edit';
  document.getElementById('add-title').textContent='Edit Task';
  document.getElementById('add-submit').textContent='Save Changes';
  applyModeVisibility();
  document.getElementById('add-desc').value = t.description || '';
  document.getElementById('add-project').value = t.project || '';
  renderUdaFields();
  document.getElementById('add-priority').value = t.priority || '';
  document.getElementById('add-due').value = twToLocalInput(t.due);
  document.getElementById('add-scheduled').value = twToLocalInput(t.scheduled);
  document.getElementById('add-wait').value = twToLocalInput(t.wait);
  document.getElementById('add-until').value = twToLocalInput(t.until);
  document.getElementById('add-recur').value = t.recur || '';
  document.getElementById('add-tags').value = (t.tags||[]).join(', ');
  meta.udas.forEach(u => { const el=document.getElementById(`uda-${u.name}`); if (el && t[u.name]!==undefined) el.value = (u.type==='date'? twToLocalInput(t[u.name]) : t[u.name]); });
  editOriginalTags = (t.tags||[]).slice();
  document.getElementById('add-modal').classList.add('open');
  setTimeout(()=>document.getElementById('add-desc').focus(),50);
}
let editOriginalTags = [];
function closeAdd(){ document.getElementById('add-modal').classList.remove('open'); }

function fillDepSelect() {
  const sel = document.getElementById('dep-select');
  sel.innerHTML = '<option value="">— choose a task —</option>' +
    depPickerTasks.filter(t => t.uuid!==editUuid)
      .map(t => `<option value="${t.uuid}">#${t.id||'?'} ${esc((t.description||'').slice(0,60))}</option>`).join('');
}
function renderDepChips() {
  document.getElementById('dep-chips').innerHTML = pendingDeps.map((d,i) =>
    `<span class="dep-chip">${esc(d.label)} <span class="x" data-i="${i}">✕</span></span>`).join('');
  document.querySelectorAll('#dep-chips .x').forEach(x => x.addEventListener('click', () => { pendingDeps.splice(+x.dataset.i,1); renderDepChips(); }));
}
function renderAnnoEditor() {
  document.getElementById('anno-list').innerHTML = pendingAnnos.map((a,i) =>
    `<div class="anno-item"><span class="txt">${esc(a)}</span><span class="x" data-i="${i}">✕</span></div>`).join('');
  document.querySelectorAll('#anno-list .x').forEach(x => x.addEventListener('click', () => { pendingAnnos.splice(+x.dataset.i,1); renderAnnoEditor(); }));
}

async function submitAdd() {
  const desc = document.getElementById('add-desc').value.trim();
  if (!desc) { toast('Description is required','error'); return; }
  const project = document.getElementById('add-project').value.trim();
  const priority = document.getElementById('add-priority').value;
  const tags = document.getElementById('add-tags').value.split(',').map(s=>s.trim()).filter(Boolean);
  const due = localToTW(document.getElementById('add-due').value);
  const scheduled = localToTW(document.getElementById('add-scheduled').value);
  const wait = localToTW(document.getElementById('add-wait').value);
  const until = localToTW(document.getElementById('add-until').value);
  const recur = document.getElementById('add-recur').value.trim();
  const uda = collectUdas();

  if (recur && !due && modalMode!=='edit') { toast('Recurring tasks need a due date','error'); return; }

  try {
    if (modalMode==='log') {
      await api('POST','/api/tasks/log',{ description:desc, project, priority, due, tags, uda });
      toast('Task logged');
    } else if (modalMode==='edit') {
      const orig = new Set(editOriginalTags);
      const now = new Set(tags);
      const payload = {
        description: desc, project, priority,
        due, scheduled, wait, until, recur, uda,
        tags_add: tags.filter(t=>!orig.has(t)),
        tags_remove: editOriginalTags.filter(t=>!now.has(t)),
      };
      await api('PATCH', `/api/tasks/${editUuid}`, payload);
      toast('Changes saved');
    } else {
      await api('POST','/api/tasks',{
        description:desc, project, priority, due, scheduled, wait, until, recur,
        tags, depends: pendingDeps.map(d=>d.uuid), annotations: pendingAnnos, uda,
      });
      toast('Task added');
    }
    closeAdd(); refresh();
  } catch {}
}

// ─── CONTEXT MODAL ──────────────────────────────────
function openCtx(){ document.getElementById('ctx-name').value=''; document.getElementById('ctx-filter').value=''; document.getElementById('ctx-modal').classList.add('open'); }
function closeCtx(){ document.getElementById('ctx-modal').classList.remove('open'); }

// ─── REPORTS ────────────────────────────────────────
function showReports() {
  document.getElementById('task-list-wrap').classList.add('hidden');
  document.getElementById('reports-view').classList.remove('hidden');
  document.getElementById('view-title').textContent='Reports';
  renderReports();
}
function hideReports() {
  document.getElementById('reports-view').classList.add('hidden');
  document.getElementById('task-list-wrap').classList.remove('hidden');
}
async function renderReports() {
  const s = meta._stats || {};
  const cards = [
    {l:'Pending',c:'',n:s.pending}, {l:'Active',c:'act',n:s.active}, {l:'Overdue',c:'over',n:s.overdue},
    {l:'Blocked',c:'blk',n:s.blocked}, {l:'Blocking',c:'',n:s.blocking}, {l:'Scheduled',c:'',n:s.scheduled},
    {l:'Waiting',c:'',n:s.waiting}, {l:'Recurring',c:'',n:s.recurring}, {l:'Completed',c:'',n:s.completed},
    {l:'Deleted',c:'',n:s.deleted},
  ];
  document.getElementById('rep-cards').innerHTML = cards.map(c =>
    `<div class="rep-card ${c.c}"><div class="n">${c.n ?? 0}</div><div class="l">${c.l}</div></div>`).join('');

  try {
    const sum = await api('GET','/api/reports/summary');
    document.getElementById('summary-rows').innerHTML = sum.length ? sum.map(r =>
      `<div class="prog-row"><div class="pname">${esc(r.project)}</div><div class="prog-track"><div class="prog-fill" style="width:${r.pct}%"></div></div><div class="prog-meta">${r.completed}/${r.total} · ${r.pct}%</div></div>`).join('')
      : '<span style="color:var(--text-muted)">No projects yet.</span>';
  } catch {}
  try { drawBurndown(await api('GET','/api/reports/burndown?days=30')); } catch {}
}
function drawBurndown(series) {
  const cv = document.getElementById('burndown'); if (!cv) return;
  const ctx = cv.getContext('2d'); const W=cv.width, H=cv.height;
  ctx.clearRect(0,0,W,H);
  if (!series || !series.length) return;
  const pad={l:30,r:10,t:10,b:22};
  const maxV = Math.max(1, ...series.map(d => Math.max(d.added, d.completed)));
  const plotW=W-pad.l-pad.r, plotH=H-pad.t-pad.b;
  const css = getComputedStyle(document.documentElement);
  const cBorder=css.getPropertyValue('--border').trim(), cMuted=css.getPropertyValue('--text-muted').trim();
  const cGreen=css.getPropertyValue('--green').trim(), cAmber=css.getPropertyValue('--accent-dim').trim();
  ctx.strokeStyle=cBorder; ctx.fillStyle=cMuted; ctx.lineWidth=1; ctx.font='10px monospace';
  for (let i=0;i<=4;i++){ const y=pad.t+plotH*(i/4); const val=Math.round(maxV*(1-i/4));
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.globalAlpha=0.35; ctx.stroke(); ctx.globalAlpha=1;
    ctx.fillText(String(val), 4, y+3); }
  const n=series.length; const slot=plotW/n; const bw=Math.max(2, Math.min(10, slot*0.36));
  series.forEach((d,i) => {
    const x=pad.l+slot*i+slot/2;
    const ah=plotH*(d.added/maxV), ch=plotH*(d.completed/maxV);
    ctx.fillStyle=cAmber; ctx.globalAlpha=0.85; ctx.fillRect(x-bw-1, pad.t+plotH-ah, bw, ah);
    ctx.fillStyle=cGreen; ctx.globalAlpha=1; ctx.fillRect(x+1, pad.t+plotH-ch, bw, ch);
    if (i%5===0 || i===n-1){ ctx.fillStyle=cMuted; ctx.globalAlpha=0.8;
      const lbl=d.date.slice(5); ctx.fillText(lbl, x-bw, H-6); ctx.globalAlpha=1; }
  });
}

// ─── REFRESH + EVENTS ───────────────────────────────
async function refresh() {
  await loadOverview();
  if (currentViewId==='reports') renderReports(); else loadTasks();
}

document.getElementById('btn-add-top').addEventListener('click', openAdd);
document.getElementById('add-cancel').addEventListener('click', closeAdd);
document.getElementById('add-modal-close').addEventListener('click', closeAdd);
document.getElementById('add-modal').addEventListener('click', e => { if (e.target===e.currentTarget) closeAdd(); });
document.getElementById('add-submit').addEventListener('click', submitAdd);
document.getElementById('add-desc').addEventListener('keydown', e => { if (e.key==='Enter' && (e.metaKey||e.ctrlKey)) submitAdd(); });
document.querySelectorAll('#add-mode button').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
document.getElementById('dep-add-btn').addEventListener('click', () => {
  const sel=document.getElementById('dep-select'); const uuid=sel.value; if (!uuid) return;
  if (!pendingDeps.find(d=>d.uuid===uuid)) { pendingDeps.push({uuid, label:sel.options[sel.selectedIndex].text}); renderDepChips(); }
  sel.value='';
});
document.getElementById('anno-add-btn').addEventListener('click', () => {
  const i=document.getElementById('anno-input'); const v=i.value.trim(); if (!v) return; pendingAnnos.push(v); i.value=''; renderAnnoEditor();
});
document.getElementById('anno-input').addEventListener('keydown', e => { if (e.key==='Enter'){ e.preventDefault(); document.getElementById('anno-add-btn').click(); } });

document.getElementById('detail-modal-close').addEventListener('click', closeDetail);
document.getElementById('detail-modal').addEventListener('click', e => { if (e.target===e.currentTarget) closeDetail(); });

document.getElementById('context-select').addEventListener('change', async e => {
  try { await api('POST','/api/context',{ name:e.target.value }); toast(`Context: ${e.target.value}`); refresh(); } catch {}
});
document.getElementById('btn-ctx-add').addEventListener('click', openCtx);
document.getElementById('ctx-cancel').addEventListener('click', closeCtx);
document.getElementById('ctx-modal-close').addEventListener('click', closeCtx);
document.getElementById('ctx-modal').addEventListener('click', e => { if (e.target===e.currentTarget) closeCtx(); });
document.getElementById('ctx-save').addEventListener('click', async () => {
  const name=document.getElementById('ctx-name').value.trim(); const filter=document.getElementById('ctx-filter').value.trim();
  if (!name||!filter){ toast('Name and filter required','error'); return; }
  try { await api('POST','/api/contexts',{ name, filter }); toast(`Context '${name}' defined`); closeCtx(); loadOverview(); } catch {}
});

document.querySelectorAll('.stat-pill[data-go]').forEach(p => p.addEventListener('click', () => selectView(p.dataset.go)));

document.getElementById('search-box').addEventListener('input', e => { searchQuery=e.target.value; renderTasks(); });
document.getElementById('sort-select').addEventListener('change', e => { sortKey=e.target.value; loadTasks(); });
document.getElementById('filter-box').addEventListener('keydown', e => { if (e.key==='Enter'){ rawFilter=e.target.value.trim(); loadTasks(); } });

document.getElementById('btn-undo').addEventListener('click', async () => { try { await api('POST','/api/undo'); toast('Last change undone'); refresh(); } catch {} });
document.getElementById('btn-export').addEventListener('click', () => { window.location.href='/api/export'; });
document.getElementById('btn-import').addEventListener('click', () => document.getElementById('import-file').click());
document.getElementById('import-file').addEventListener('change', async e => {
  const f=e.target.files[0]; if (!f) return;
  const fd=new FormData(); fd.append('file', f);
  try { const r=await fetch('/api/import',{ method:'POST', body:fd });
    if (r.status===401) { window.location.href='/login'; }
    const d=await r.json();
    if (!r.ok) throw new Error(d.error||'import failed'); toast('Imported'); refresh(); }
  catch (err) { toast(err.message||'Import failed','error'); }
  e.target.value='';
});

document.getElementById('btn-menu').addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
document.querySelector('.sidebar').addEventListener('click', () => document.body.classList.remove('sidebar-open'));
document.getElementById('btn-logout').addEventListener('click', async () => {
  try { await fetch('/logout',{ method:'POST' }); } catch {}
  window.location.href='/login';
});

document.addEventListener('keydown', e => {
  const typing = ['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName);
  if (e.key==='Escape') { closeAdd(); closeDetail(); closeCtx(); }
  if (typing) return;
  if (e.key==='m') { document.body.classList.toggle('sidebar-open'); }
  if (e.key==='n' && !e.ctrlKey && !e.metaKey) { e.preventDefault(); openAdd(); }
  if (e.key==='/') { e.preventDefault(); document.getElementById('search-box').focus(); }
  if (e.key==='u') { document.getElementById('btn-undo').click(); }
  if (e.key==='r') { selectView('reports'); }
});

// ─── INIT ───────────────────────────────────────────
(async function init(){ await loadOverview(); loadTasks(); })();
setInterval(loadOverview, 30000);
