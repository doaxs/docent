/* Docent — client behavior.
 *
 * The server owns TV state: /status OOB-swaps the screen, info column and
 * control capsule every 10s; /library renders tiles. This script owns the
 * things that only exist in the browser: the preview-before-commit flow
 * (#screen-preview, #info-preview, matte tray), library filter/search/view,
 * popovers, drag-and-drop upload, rename/delete dialogs and toasts.
 */
(function () {
  'use strict';

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const app = $('#app');
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const icon = (n, cls = '') => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const thumb = id => `/thumb/${encodeURIComponent(id)}`;

  const S = {
    mode: 'now',          // 'now' | 'preview'
    sel: null,            // content_id being previewed
    draft: null,          // { style, color } chosen in the tray
    rejected: {},         // content_id -> Set(styles the TV refused)
    filter: 'all',
    query: '',
    view: localStorage.getItem('frame.view') === 'grid' ? 'grid' : 'tv',
    matte: { styles: [], colors: [] },
    sending: false,
    pendingPreview: null, // content_id to preview once the library re-renders
  };

  const tvState = () => $('#tv-state').dataset;
  const isLive = () => tvState().state === 'connected';
  const currentId = () => tvState().current || $('#lib-data').dataset.current || '';
  const sortValue = () => $('#sort').value;
  const tileEl = id => $(`.tile[data-id="${CSS.escape(id)}"]`);
  const tileData = id => { const t = tileEl(id); return t ? { ...t.dataset, style: t.dataset.mstyle, color: t.dataset.mcolor } : null; };
  const colorHex = key => (S.matte.colors.find(c => c.key === key) || {}).hex || '#f2f2ee';
  const styleName = key => (S.matte.styles.find(s => s.key === key) || {}).name || key;
  const colorName = key => (S.matte.colors.find(c => c.key === key) || {}).name || key;

  /* =======================================================================
     Toasts
     ======================================================================= */
  function toast(msg, kind = 'info', opts = {}) {
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    const ic = kind === 'ok' ? 'check' : kind === 'err' ? 'warn' : 'tv';
    const lead = opts.spinner ? '<span class="t-ic"><span class="spinner"></span></span>'
      : opts.thumb ? `<span class="t-th" style="background-image:url('${opts.thumb}')"></span>`
      : `<span class="t-ic">${icon(ic)}</span>`;
    t.innerHTML = `${lead}<div>${esc(msg)}${opts.sub ? `<small>${esc(opts.sub)}</small>` : ''}</div>`;
    $('#toasts').appendChild(t);
    const close = () => { t.classList.add('leaving'); setTimeout(() => t.remove(), 300); };
    if (!opts.sticky) setTimeout(close, opts.ms || 3800);
    return close;
  }

  function errorDetail(text, status) {
    try { const b = JSON.parse(text); if (b && b.detail) return String(b.detail); } catch (_) { /* not JSON */ }
    return `Something went wrong (${status}).`;
  }

  document.body.addEventListener('htmx:responseError', e => {
    toast(errorDetail(e.detail.xhr.responseText, e.detail.xhr.status), 'err', { ms: 5200 });
  });
  document.body.addEventListener('htmx:sendError', () => toast('Can’t reach the dashboard server.', 'err'));
  document.body.addEventListener('htmx:targetError', () => toast('Part of the page is missing — try reloading.', 'err'));

  /* Mutations that need to coordinate with client state go through fetch;
     the response is the library partial, swapped like htmx would. */
  async function send(method, url, body) {
    let r;
    try { r = await fetch(url, { method, body }); }
    catch (_) { toast('Can’t reach the dashboard server.', 'err'); return null; }
    const text = await r.text();
    if (!r.ok) { toast(errorDetail(text, r.status), 'err', { ms: 5200 }); return null; }
    if (text.trim()) { htmx.swap('#library', text, { swapStyle: 'innerHTML' }); onLibrarySwapped(); }
    (r.headers.get('HX-Trigger') || '').split(',').map(s => s.trim()).filter(Boolean)
      .forEach(ev => htmx.trigger(document.body, ev));
    return r;
  }
  const form = obj => { const f = new FormData(); Object.entries(obj).forEach(([k, v]) => v != null && f.append(k, v)); return f; };

  /* =======================================================================
     Matte rendering — mirrors the matte() macro in partials/macros.html
     ======================================================================= */
  const BEVEL = ['modern', 'modernthin', 'modernwide', 'squares', 'panoramic'];
  const MULTI = ['triptych', 'mix'];
  function matteHTML(src, style, hex) {
    const bg = `background-image:url('${src}')`;
    const inner = MULTI.includes(style)
      ? `<div class="a">${`<div class="p" style="${bg}"></div>`.repeat(3)}</div>`
      : `<div class="a${BEVEL.includes(style) ? ' bevel' : ''}" style="${bg}"></div>`;
    return `<div class="m m-${style}" style="--matte:${style === 'none' ? '#000' : hex}">${inner}</div>`;
  }

  /* =======================================================================
     Glow behind the TV — a blurred copy of whatever the screen shows
     ======================================================================= */
  let glowNow = null;
  function setGlow(src) {
    if (src === glowNow) return;
    const g = $('#glow');
    $$('.glow-img', g).forEach(el => { el.classList.add('leaving'); setTimeout(() => el.remove(), 650); });
    if (src) {
      const el = document.createElement('div');
      el.className = 'glow-img';
      el.style.backgroundImage = `url('${src}')`;
      g.appendChild(el);
    }
    glowNow = src;
  }
  function syncGlow() {
    if (S.mode === 'preview' && S.sel) setGlow(thumb(S.sel));
    else setGlow($('#screen-now').dataset.glow || null);
  }

  /* =======================================================================
     Status poll: skip no-op swaps, react to connection changes
     ======================================================================= */
  const keyOf = node => node && (node.getAttribute?.('data-key') ?? node.firstElementChild?.getAttribute?.('data-key'));
  document.body.addEventListener('htmx:oobBeforeSwap', e => {
    const { target, fragment } = e.detail;
    // Don't clobber an in-progress rename.
    if (target.contains(document.activeElement) && document.activeElement.matches('input')) { e.detail.shouldSwap = false; return; }
    const incoming = keyOf(fragment);
    if (incoming != null && target.dataset.key === incoming) e.detail.shouldSwap = false;
  });

  document.body.addEventListener('htmx:oobAfterSwap', e => {
    if (e.target.id === 'tv-state') onStateChanged();
    if (e.target.id === 'screen-now') syncGlow();
  });

  function onStateChanged() {
    const st = tvState().state;
    app.dataset.state = st;
    $('#add-btn').disabled = st !== 'connected';
    $('#lib-note').hidden = st !== 'unreachable';
    if (st !== 'connected' && S.mode === 'preview') exitPreview();
    syncSideNow();
    syncGlow();
  }

  /* =======================================================================
     Library: filter, search, counts, view, selection
     ======================================================================= */
  function onLibrarySwapped() {
    try { S.matte = JSON.parse($('#lib-data').dataset.matte || '{}'); } catch (_) { /* keep last */ }
    S.matte.styles = S.matte.styles || []; S.matte.colors = S.matte.colors || [];
    applyFilter();
    syncTileSel();
    syncSideNow();
    if (S.pendingPreview && tileEl(S.pendingPreview)) {
      const id = S.pendingPreview; S.pendingPreview = null;
      preview(id); tileEl(id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } else if (S.mode === 'preview') {
      if (!tileEl(S.sel)) exitPreview(); else renderPreview();
    }
  }
  document.body.addEventListener('htmx:afterSwap', e => {
    if (e.detail.target?.id === 'library') onLibrarySwapped();
    if (e.detail.target?.id === 'pop-settings') syncSeg($('#theme-seg'));
  });

  function applyFilter() {
    const q = S.query.trim().toLowerCase();
    let shown = 0;
    $$('#library .tile[data-id]').forEach(t => {
      const store = t.dataset.store === '1';
      const ok = (S.filter === 'all' || (S.filter === 'store') === store) &&
                 (!q || t.dataset.name.toLowerCase().includes(q));
      t.classList.toggle('filtered', !ok);
      if (ok) shown++;
    });
    const total = $$('#library .tile[data-id]').length;
    $('#lib-count').textContent = !total ? '' : q ? `${shown} ${shown === 1 ? 'match' : 'matches'}` : `${shown} ${shown === 1 ? 'piece' : 'pieces'}`;
    const noMatch = total > 0 && shown === 0;
    $('#no-match').hidden = !noMatch;
    $('#no-match-q').textContent = S.query || (S.filter === 'store' ? 'Art Store' : 'My Photos');
    $('#lib-title').textContent = S.view === 'grid'
      ? ({ all: 'All Art', mine: 'My Photos', store: 'Art Store' })[S.filter] : 'Library';
    $('.search').classList.toggle('has-q', !!q);
  }

  function setFilter(f) {
    S.filter = f;
    $$('[data-f]').forEach(b => b.classList.toggle('on', b.dataset.f === f));
    syncSeg($('#filter-seg'));
    applyFilter();
    $('#library').scrollTo({ left: 0, top: 0 });
  }

  function setView(v, { focusSel = false } = {}) {
    S.view = v;
    app.dataset.view = v;
    localStorage.setItem('frame.view', v);
    $$('#view-seg [data-view]').forEach(b => b.classList.toggle('on', b.dataset.view === v));
    syncSeg($('#view-seg'));
    syncSeg($('#filter-seg'));
    applyFilter();
    const target = focusSel ? (tileEl(S.sel || '') || tileEl(currentId())) : null;
    requestAnimationFrame(() => target?.scrollIntoView({ block: 'nearest' }));
  }

  function syncSeg(seg) {
    if (!seg || !seg.offsetParent) return;
    const on = seg.querySelector('button.on'), th = seg.querySelector('.thumb');
    if (!on || !th) return;
    th.style.left = on.offsetLeft + 'px';
    th.style.width = on.offsetWidth + 'px';
  }

  function syncTileSel() {
    $$('#library .tile').forEach(t => t.classList.toggle('sel', S.mode === 'preview' && t.dataset.id === S.sel));
  }

  function syncSideNow() {
    const id = currentId(), d = id && tileData(id);
    $('#side-now-th').innerHTML = d ? matteHTML(thumb(id), d.style, colorHex(d.color)) : '';
    $('#side-now-name').textContent = d ? d.name : (isLive() ? 'Art Mode' : 'Not connected');
  }

  /* =======================================================================
     Preview — see it on the TV render before committing
     ======================================================================= */
  function preview(id) {
    if (!isLive()) return;
    const d = tileData(id); if (!d) return;
    if (S.view === 'grid') setView('tv');
    S.mode = 'preview'; S.sel = id; S.draft = { style: d.style, color: d.color };
    app.dataset.mode = 'preview';
    closePops();
    renderPreview();
    syncTileSel();
    syncGlow();
  }

  function exitPreview() {
    S.mode = 'now'; S.sel = null; S.draft = null; S.sending = false;
    app.dataset.mode = 'now';
    syncTileSel();
    syncGlow();
    setTimeout(() => { if (S.mode === 'now') $('#screen-preview').innerHTML = ''; }, 320);
  }

  function renderPreview() {
    renderPreviewScreen();
    renderPreviewInfo();
    renderTray();
  }

  function renderPreviewScreen() {
    const host = $('#screen-preview'), id = S.sel, d = S.draft;
    const hex = colorHex(d.color);
    const layer = host.querySelector('.layer:last-child');
    if (layer && layer.dataset.id === id) {
      // Same artwork, matte change → morph in place so the opening resizes smoothly.
      const m = layer.querySelector('.m'), a = m && m.querySelector('.a');
      const was = m && [...m.classList].find(c => c.startsWith('m-') && c !== 'm').slice(2);
      if (m && a && !MULTI.includes(was) && !MULTI.includes(d.style)) {
        m.className = `m m-${d.style}`;
        m.style.setProperty('--matte', d.style === 'none' ? '#000' : hex);
        a.className = `a${BEVEL.includes(d.style) ? ' bevel' : ''}`;
      } else {
        layer.innerHTML = matteHTML(thumb(id), d.style, hex);
      }
      return;
    }
    host.querySelectorAll('.layer').forEach(l => setTimeout(() => l.remove(), 420));
    const el = document.createElement('div');
    el.className = 'layer';
    el.dataset.id = id;
    el.innerHTML = matteHTML(thumb(id), d.style, hex);
    host.appendChild(el);
  }

  function renderPreviewInfo() {
    const id = S.sel, d = tileData(id), cur = currentId();
    if (!d) return;
    const same = id === cur, store = d.store === '1';
    const changed = !same || d.style !== S.draft.style || d.color !== S.draft.color;
    const curData = cur && tileData(cur);
    const label = same ? 'Apply to TV' : 'Show on TV';
    $('#info-preview').innerHTML = `
      <div class="eyebrow preview">${icon('eye')}${same ? 'Adjusting matte' : 'Preview'}</div>
      <h1 class="title" ${store ? '' : `data-act="rename" data-id="${esc(id)}" title="Click to rename"`}>${esc(d.name)}</h1>
      <div class="meta">${store ? '<span class="badge-store">ART STORE</span><span>Samsung collection</span>'
        : `<span>Your photo</span>${d.ago ? `<span class="sep">·</span><span>Added ${esc(d.ago)}</span>` : ''}`}</div>
      <div class="actions">
        <button type="button" class="btn primary big${S.sending ? ' sending' : ''}" data-act="commit" ${changed ? '' : 'disabled'}>
          ${S.sending ? '<span class="spinner"></span>Sending…' : icon('tv') + label}</button>
        <button type="button" class="btn big" data-act="cancel">Cancel</button>
        <button type="button" class="btn big icon" data-act="menu" data-id="${esc(id)}" aria-label="More">${icon('dots')}</button>
      </div>
      ${same ? '<div class="replaces">Pick a matte below, then apply it to your TV.</div>'
        : curData ? `<div class="replaces">Replaces <span class="rt" style="background-image:url('${thumb(cur)}')"></span><b>${esc(curData.name)}</b></div>` : ''}
      <div class="hint-kbd"><kbd>←</kbd><kbd>→</kbd> browse <kbd>return</kbd> show <kbd>esc</kbd> cancel</div>`;
  }

  function renderTray() {
    const id = S.sel, d = S.draft, rej = S.rejected[id] || new Set();
    $('#styles').innerHTML = S.matte.styles.map(({ key, name }) => `
      <button type="button" class="style-btn${d.style === key ? ' on' : ''}" data-style="${key}" title="${esc(name)}${rej.has(key) ? ' — your TV refused this one' : ''}">
        <span class="mini">${rej.has(key) ? '<span class="warn-dot">!</span>' : ''}${matteHTML(thumb(id), key, colorHex(d.color))}</span><span>${esc(name)}</span>
      </button>`).join('');
    const off = d.style === 'none';
    $('#colors').className = 'colors' + (off ? ' disabled' : '');
    $('#colors').innerHTML = S.matte.colors.map(({ key, name, hex }) =>
      `<button type="button" class="sw${d.color === key && !off ? ' on' : ''}" style="--c:${hex}" data-color="${key}" title="${esc(name)}" aria-label="${esc(name)}"></button>`).join('');
    $('#color-name').innerHTML = off ? '<small>No matte</small>' : esc(colorName(d.color));
  }

  function setDraft(patch) {
    Object.assign(S.draft, patch);
    renderPreviewScreen(); renderTray(); renderPreviewInfo();
  }

  async function commit() {
    if (S.sending || S.mode !== 'preview') return;
    const id = S.sel, d = { ...S.draft }, same = id === currentId();
    const name = tileData(id)?.name || '';
    S.sending = true; renderPreviewInfo();
    const r = await send('POST', `/image/${encodeURIComponent(id)}/show`,
      form({ matte_type: d.style, color: d.color, sort: sortValue() }));
    S.sending = false;
    if (!r) {
      if (S.sel === id) {
        (S.rejected[id] = S.rejected[id] || new Set()).add(d.style);
        renderPreviewInfo(); renderTray();
      }
      return;
    }
    exitPreview();
    toast(same ? 'Matte updated on your TV' : 'Now on your TV', 'ok', {
      thumb: thumb(id),
      sub: same ? (d.style === 'none' ? 'No matte' : `${styleName(d.style)} · ${colorName(d.color)}`) : name,
    });
  }

  function step(dir) {
    const list = $$('#library .tile[data-id]:not(.filtered)');
    const i = list.findIndex(t => t.dataset.id === S.sel);
    const next = list[(i + dir + list.length) % list.length];
    if (!next) return;
    preview(next.dataset.id);
    next.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }

  /* =======================================================================
     Popovers + menu
     ======================================================================= */
  function closePops(except) {
    if (except !== 'settings') { $('#pop-settings').classList.remove('open'); $('#gear-btn').classList.remove('is-on'); }
    if (except !== 'slideshow') $('#pop-slideshow').classList.remove('open');
    $('#menu').classList.remove('open');
  }

  function openSettings({ focusIp = false } = {}) {
    closePops('settings');
    const p = $('#pop-settings');
    const show = () => {
      p.classList.add('open'); $('#gear-btn').classList.add('is-on');
      syncSeg($('#theme-seg'));
      applyBezel();
      if (focusIp) setTimeout(() => p.querySelector('input[name="tv_ip"]')?.focus(), 150);
    };
    htmx.ajax('GET', '/settings', { target: p, swap: 'innerHTML' }).then(show);
  }

  function openSlideshow() {
    if (!isLive()) { openSettings(); return; }
    closePops('slideshow');
    const p = $('#pop-slideshow');
    htmx.ajax('GET', '/slideshow', { target: p, swap: 'innerHTML' }).then(() => {
      const row = $('#slide-row').getBoundingClientRect(), cap = $('#capsule').getBoundingClientRect();
      p.style.right = (window.innerWidth - cap.left + 12) + 'px';
      p.style.top = Math.max(64, Math.min(window.innerHeight - p.offsetHeight - 16, row.top + row.height / 2 - p.offsetHeight / 2)) + 'px';
      p.classList.add('open');
    });
  }

  function openMenu(btn) {
    const id = btn.dataset.id, d = tileData(id) || {}, store = d.store === '1';
    const m = $('#menu');
    m.className = 'popover menu glass';
    m.innerHTML = `
      <button type="button" data-act="rename" data-id="${esc(id)}" ${store ? 'disabled' : ''}>${icon('pencil')}Rename…</button>
      <button type="button" data-act="adjust" data-id="${esc(id)}">${icon('frame')}Adjust Matte</button>
      <hr>
      <button type="button" data-act="delete" data-id="${esc(id)}" class="danger" ${store ? 'disabled' : ''}>${icon('trash')}Delete from TV…</button>
      ${store ? '<div class="mi-note">Art Store pieces can’t be renamed or deleted.</div>' : ''}`;
    // Anchor under the button, but keep the whole menu inside the window:
    // flip to right-aligned near the right edge, and above when too low.
    const r = btn.getBoundingClientRect(), pad = 12;
    const w = m.offsetWidth, h = m.offsetHeight;
    const flipX = r.left + w > window.innerWidth - pad;
    const flipY = r.bottom + 6 + h > window.innerHeight - pad;
    const left = flipX ? Math.max(pad, r.right - w) : r.left;
    const top = flipY ? Math.max(pad, r.top - 6 - h) : r.bottom + 6;
    m.style.left = left + 'px';
    m.style.top = top + 'px';
    m.style.transformOrigin = `${flipX ? w - 18 : 18}px ${flipY ? h : 0}px`;
    requestAnimationFrame(() => m.classList.add('open'));
  }

  /* =======================================================================
     Rename + delete
     ======================================================================= */
  function startRename(id) {
    const host = S.mode === 'preview' ? $('#info-preview') : $('#info-now');
    const h = host.querySelector('.title'), d = tileData(id);
    if (!h || !d || d.store === '1') return;
    const inp = document.createElement('input');
    inp.className = 'title-input'; inp.value = d.name; inp.setAttribute('aria-label', 'Name');
    h.replaceWith(inp); inp.focus(); inp.select();
    let done = false;
    const finish = async save => {
      if (done) return; done = true;
      const name = inp.value.trim();
      if (!save || !name || name === d.name) { inp.replaceWith(h); return; }
      h.textContent = name; inp.replaceWith(h);
      const r = await send('POST', `/image/${encodeURIComponent(id)}/rename`, form({ name, sort: sortValue() }));
      if (r) toast('Renamed', 'ok', { sub: name });
      else h.textContent = d.name;
    };
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
  }

  let pendingDelete = null;
  function askDelete(id) {
    const d = tileData(id); if (!d || d.store === '1') return;
    pendingDelete = id;
    $('#al-th').style.backgroundImage = `url('${thumb(id)}')`;
    $('#al-title').textContent = `Delete “${d.name}”?`;
    $('#al-body').textContent = id === currentId()
      ? 'It’s on your TV right now — the TV will switch to another piece. This can’t be undone.'
      : 'This removes it from your Frame. This can’t be undone.';
    $('#scrim').classList.add('open');
    setTimeout(() => $('#al-cancel').focus(), 50);
  }
  function closeDelete() { pendingDelete = null; $('#scrim').classList.remove('open'); }
  async function doDelete() {
    const id = pendingDelete, name = tileData(id)?.name || '';
    closeDelete();
    if (S.sel === id) exitPreview();
    const r = await send('DELETE', `/image/${encodeURIComponent(id)}?sort=${encodeURIComponent(sortValue())}`);
    if (r) toast('Deleted from your TV', 'info', { sub: name });
  }

  /* =======================================================================
     Upload — Add Art button or drop anywhere
     ======================================================================= */
  async function addFiles(fileList) {
    const files = [...fileList].filter(f => f.type.startsWith('image/') || /\.(heic|heif)$/i.test(f.name));
    if (!files.length) { toast('Those aren’t images', 'err', { sub: 'Drop JPEG, PNG or HEIC photos.' }); return; }
    if (!isLive()) { toast('Your TV isn’t connected', 'err', { sub: 'Wake it first, then add your photos.' }); return; }
    const many = files.length > 1;
    const closeBusy = toast(many ? `Sending ${files.length} photos to your TV…` : 'Sending to your TV…', 'info',
      { spinner: true, sticky: true, sub: 'Fitting to 4K and uploading' });
    $('#sort').value = 'newest';
    let lastId = null, added = 0, dup = false;
    for (const f of files) {
      const fd = new FormData(); fd.append('file', f, f.name); fd.append('sort', 'newest');
      const r = await send('POST', '/upload', fd);
      if (!r) continue;
      lastId = r.headers.get('X-Content-Id') || lastId;
      if ((r.headers.get('HX-Trigger') || '').includes('duplicateUpload')) dup = true; else added++;
    }
    closeBusy();
    if (!lastId) return;
    if (S.filter === 'store') setFilter('all');
    if (dup && !added) {
      toast('Already on your TV — switched to it', 'info', { thumb: thumb(lastId) });
    } else {
      toast(many ? `Added ${added} photos to your TV` : 'Added to your TV', 'ok', { thumb: thumb(lastId), sub: 'Previewing — press Show on TV when ready' });
      if (tileEl(lastId)) { preview(lastId); tileEl(lastId).scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
      else S.pendingPreview = lastId;
    }
  }
  document.body.addEventListener('duplicateUpload', () => { /* toast handled in addFiles */ });

  $('#add-btn').addEventListener('click', () => $('#file-input').click());
  $('#file-input').addEventListener('change', e => { addFiles(e.target.files); e.target.value = ''; });

  let dragDepth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', e => {
    if (!hasFiles(e) || !isLive()) return;
    e.preventDefault(); dragDepth++; $('#drop').classList.add('open');
  });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#drop').classList.remove('open'); } });
  window.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; $('#drop').classList.remove('open');
    addFiles(e.dataTransfer.files);
  });

  /* =======================================================================
     Appearance
     ======================================================================= */
  function applyTheme() {
    const pref = localStorage.getItem('frame.theme');
    const t = pref === 'light' || pref === 'dark' ? pref : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = t;
    $$('#theme-seg [data-theme-set]').forEach(b => b.classList.toggle('on', b.dataset.themeSet === (pref || 'system')));
    syncSeg($('#theme-seg'));
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

  // TV bezel — display-only, matches the snap-on bezel on your Frame.
  function applyBezel() {
    const b = localStorage.getItem('frame.bezel') || 'black';
    document.documentElement.dataset.bezel = b;
    $$('#bezel-set [data-bezel-set]').forEach(btn => btn.classList.toggle('on', btn.dataset.bezelSet === b));
  }
  document.body.addEventListener('htmx:afterSettle', e => {
    if (e.detail.target?.id === 'pop-settings') { applyTheme(); applyBezel(); }
  });

  /* =======================================================================
     Events
     ======================================================================= */
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-act], [data-style], [data-color], [data-f], [data-view], [data-theme-set], [data-bezel-set], .tile[data-id]');
    if (!e.target.closest('.popover') && !e.target.closest('#gear-btn') && !(t && ['slideshow', 'menu', 'settings'].includes(t.dataset.act))) closePops();
    if (!t || t.disabled) return;

    if (t.dataset.style) { setDraft({ style: t.dataset.style }); return; }
    if (t.dataset.color) { setDraft({ color: t.dataset.color }); return; }
    if (t.dataset.f) { setFilter(t.dataset.f); return; }
    if (t.dataset.view) { setView(t.dataset.view, { focusSel: true }); return; }
    if (t.dataset.bezelSet) { localStorage.setItem('frame.bezel', t.dataset.bezelSet); applyBezel(); return; }
    if (t.dataset.themeSet) {
      if (t.dataset.themeSet === 'system') localStorage.removeItem('frame.theme');
      else localStorage.setItem('frame.theme', t.dataset.themeSet);
      applyTheme(); return;
    }
    if (!t.dataset.act && t.classList.contains('tile')) {
      if (e.target.closest('.quick')) return; // htmx handles quick-show
      if (t.classList.contains('skel')) return;
      preview(t.dataset.id); return;
    }
    const id = t.dataset.id;
    switch (t.dataset.act) {
      case 'commit': commit(); break;
      case 'cancel': exitPreview(); break;
      case 'adjust': closePops(); preview(id || currentId()); break;
      case 'menu': $('#menu').classList.contains('open') ? closePops() : openMenu(t); break;
      case 'rename': closePops(); startRename(id); break;
      case 'delete': closePops(); askDelete(id); break;
      case 'slideshow': $('#pop-slideshow').classList.contains('open') ? closePops() : openSlideshow(); break;
      case 'settings': openSettings({ focusIp: true }); break;
      case 'back-to-tv': setView('tv', { focusSel: true }); break;
    }
  });

  $('#gear-btn').addEventListener('click', e => {
    // hx-get on the button reloads the panel; we just toggle visibility.
    const p = $('#pop-settings');
    if (p.classList.contains('open')) { closePops(); e.preventDefault(); return; }
    closePops('settings');
    p.classList.add('open'); $('#gear-btn').classList.add('is-on');
  });

  document.body.addEventListener('htmx:afterRequest', e => {
    const elt = e.detail.elt;
    if (!e.detail.successful || !elt?.classList) return;
    if (elt.classList.contains('quick')) {
      const id = elt.closest('.tile')?.dataset.id;
      if (S.mode === 'preview') exitPreview();
      htmx.trigger(document.body, 'refreshStatus');
      toast('Now on your TV', 'ok', { thumb: id ? thumb(id) : undefined, sub: id ? tileData(id)?.name : '' });
    }
    if (elt.matches('[hx-post="/wake"]')) toast('Waking your TV…', 'info', { sub: 'It can take 10–20 seconds to come back.' });
    if (elt.matches('[hx-post="/power/off"]')) toast('Turning your TV off', 'info');
    if (elt.matches('[hx-post="/pair/reset"]')) toast('Look at your TV', 'info', { sub: 'Press Allow when the prompt appears.' });
    if (elt.closest('#pop-slideshow form')) htmx.trigger(document.body, 'refreshStatus');
    if (elt.closest('.found') || elt.matches('#pop-settings form')) { closePops(); toast('Connecting to your TV…', 'info', { sub: 'Watch the TV for an Allow prompt.' }); }
  });

  $('#search').addEventListener('input', e => { S.query = e.target.value; applyFilter(); });
  $('#search').addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.target.value = ''; S.query = ''; applyFilter(); e.target.blur(); e.stopPropagation(); }
    if (e.key === 'Enter') { const first = $('#library .tile[data-id]:not(.filtered)'); if (first) { e.target.blur(); preview(first.dataset.id); } }
  });

  $('#library').addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target.classList.contains('tile')) preview(e.target.dataset.id);
  });

  $('#al-cancel').addEventListener('click', closeDelete);
  $('#al-ok').addEventListener('click', doDelete);
  $('#scrim').addEventListener('click', e => { if (e.target.id === 'scrim') closeDelete(); });

  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); $('#search').focus(); return; }
    if (e.target.matches('input, select, textarea')) return;
    if (e.key === 'Escape') {
      if ($('#scrim').classList.contains('open')) { closeDelete(); return; }
      if ($('.popover.open')) { closePops(); return; }
      if (S.mode === 'preview') { exitPreview(); return; }
      if (S.view === 'grid') { setView('tv'); return; }
    }
    if (S.mode === 'preview' && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) { e.preventDefault(); step(e.key === 'ArrowRight' ? 1 : -1); }
    if (S.mode === 'preview' && e.key === 'Enter' && !e.target.closest('.tile, button')) commit();
  });

  window.addEventListener('resize', () => $$('.seg').forEach(syncSeg));

  /* ---------- boot ---------- */
  setView(S.view);
  applyTheme();
  applyBezel();
})();
