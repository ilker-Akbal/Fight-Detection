(() => {
  'use strict';
  const endpoint = document.body.dataset.overviewUrl;
  let timer, refreshing = false, closed = false, pending = null;
  const setStatus = (node, value) => {
    if (!node) return;
    node.textContent = value.label;
    node.className = `badge ${value.tone}`;
  };
  const controlButtons = () => document.querySelectorAll('form[data-control] button');
  function finish(message) {
    pending = null;
    controlButtons().forEach(button => button.disabled = false);
    const result = document.querySelector('[data-control-result]');
    if (result) result.textContent = message;
  }
  // A single page stream avoids the browser's per-origin connection ceiling.
  // Reconnect delay is failure-only; delivered frames have no polling interval.
  const tiles = new Map([...document.querySelectorAll('[data-preview]')].map(img => [
    img.closest('[data-camera]').dataset.camera, {img, enabled: img.dataset.monitorable === 'true', last: 0, url: null, next: null, decoding: false}
  ]));
  let previewController, previewRetry, previewRunning = false, previewLastRead = 0;
  function disconnectPreviews() {
    clearTimeout(previewRetry); previewController?.abort();
    tiles.forEach(tile => {tile.last = 0; tile.img.hidden = true; tile.next = null;
      if (tile.url) URL.revokeObjectURL(tile.url); tile.url = null;});
  }
  async function display(tile) {
    if (tile.decoding || !tile.next) return;
    tile.decoding = true;
    const data = tile.next; tile.next = null;
    const nextUrl = URL.createObjectURL(new Blob([data], {type: 'image/jpeg'}));
    const previous = tile.url; tile.url = nextUrl; tile.img.src = nextUrl;
    try {
      await tile.img.decode();
      if (!closed && !document.hidden && tile.enabled) {
        tile.last = Date.now(); tile.img.hidden = false;
        tile.img.parentElement.querySelector('[data-preview-empty]').hidden = true;
      }
    } catch (_) {tile.img.hidden = true;}
    finally {
      if (previous) URL.revokeObjectURL(previous);
      tile.decoding = false;
      if (tile.next) display(tile); // one pending latest JPEG, never a decode backlog
    }
  }
  async function connectPreviews() {
    if (![...tiles.values()].some(tile => tile.enabled) || closed || document.hidden || previewRunning) return;
    previewRunning = true; previewController = new AbortController();
    previewLastRead = Date.now();
    const url = new URL(document.body.dataset.previewsUrl, location.href);
    tiles.forEach((tile, camera) => {if (tile.enabled) url.searchParams.append('camera', camera);});
    try {
      const response = await fetch(url, {cache: 'no-store', signal: previewController.signal});
      if (!response.ok || !response.headers.get('Content-Type')?.startsWith('application/x-camera-frames')) throw new Error('preview unavailable');
      const parser = new CameraFrameParser((camera, jpeg) => {
        const tile = tiles.get(camera);
        if (tile) {tile.next = jpeg; display(tile);}
      });
      const reader = response.body.getReader();
      try {
        while (!closed && !document.hidden) {
          const {value, done} = await reader.read();
          if (done) break;
          previewLastRead = Date.now();
          parser.feed(value);
        }
      } finally {await reader.cancel();}
    } catch (_) { /* Status polling supplies the source error, not a frozen JPEG. */ }
    finally {
      previewRunning = false;
      if (!closed && !document.hidden) previewRetry = setTimeout(connectPreviews, 2000);
    }
  }
  connectPreviews();
  async function refresh() {
    if (!endpoint || document.hidden || refreshing || closed) return;
    refreshing = true;
    try {
      const live = document.getElementById('live-workspace');
      const response = await fetch(endpoint + (live ? '?live=1' : ''), {headers: {Accept: 'application/json'}, signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('status unavailable');
      const data = await response.json();
      if (previewRunning && Date.now() - previewLastRead > 8000) previewController.abort();
      document.querySelectorAll('#global-status,[data-system-status]').forEach(node => setStatus(node, data.system));
      document.querySelectorAll('[data-health-stale]').forEach(node => node.hidden = !data.system.stale);
      const cameras = new Map((data.cameras || []).map(camera => [camera.camera_id, camera]));
      document.querySelectorAll('[data-camera]').forEach(tile => {
        const camera = cameras.get(tile.dataset.camera);
        setStatus(tile.querySelector('[data-camera-status]'), camera || {label: 'Kamera artık erişilebilir değil', tone: 'neutral'});
        const analysis = tile.querySelector('[data-analysis]');
        if (analysis && camera) analysis.textContent = camera.analysis;
        const img = tile.querySelector('[data-preview]');
        if (img) {
          const preview = tiles.get(tile.dataset.camera);
          if (preview && preview.enabled !== Boolean(camera?.monitorable)) {
            preview.enabled = Boolean(camera?.monitorable); disconnectPreviews();
            if (!previewRunning) connectPreviews();
          }
          img.hidden = !camera || Date.now() - (tiles.get(tile.dataset.camera)?.last || 0) > 3000;
          const empty = tile.querySelector('[data-preview-empty]');
          empty.hidden = !img.hidden;
          empty.textContent = camera?.label || 'Kamera erişimi yok';
          if (!camera) {tiles.delete(tile.dataset.camera); disconnectPreviews();}
        }
      });
      document.querySelectorAll('[data-online-count]').forEach(node => node.textContent = [...cameras.values()].filter(camera => camera.tone === 'success').length);
      if (live && data.events_html !== undefined) document.getElementById('live-event-feed').innerHTML = data.events_html;
      (data.workers || []).forEach(worker => {
        const row = document.querySelector(`[data-worker="${worker.name}"]`);
        if (!row) return;
        row.querySelector('[data-worker-health]').textContent = worker.health;
        row.querySelector('[data-worker-age]').textContent = worker.heartbeat_age_sec == null ? '—' : `${worker.heartbeat_age_sec} sn`;
        row.querySelector('[data-worker-restarts]').textContent = worker.restart_count ?? '—';
      });
      if (pending) {
        if (pending.revision != null && data.system.desired_camera_revision >= pending.revision &&
            data.system.analytics_confirmed && data.system.analytics_paused === pending.paused) finish('İşlem doğrulandı.');
        else if (Date.now() > pending.deadline) finish('İşlem henüz doğrulanamadı. Sistem durumunu kontrol edin.');
      }
    } catch (_) {
      document.querySelectorAll('#global-status,[data-system-status]').forEach(node => setStatus(node, {label: 'Durum alınamıyor', tone: 'warning'}));
      if (pending && Date.now() > pending.deadline) finish('İşlem sonucu doğrulanamadı.');
    } finally {refreshing = false; if (!closed) timer = setTimeout(refresh, pending ? 1000 : 3000);}
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden) disconnectPreviews(); else connectPreviews();
    if (!document.hidden) refresh();
  });
  window.addEventListener('pagehide', () => {closed = true; clearTimeout(timer); disconnectPreviews();});
  window.addEventListener('pageshow', event => {if (event.persisted) {closed = false; connectPreviews(); refresh();}});
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!confirm(form.dataset.confirm)) {event.preventDefault(); event.stopImmediatePropagation();}
  }));
  document.querySelectorAll('form[data-control]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending) return;
    pending = {paused: form.dataset.control === 'stop', deadline: Date.now() + 60000};
    controlButtons().forEach(button => button.disabled = true);
    document.querySelector('[data-control-result]').textContent = pending.paused ? 'Analizler durduruluyor…' : 'Analizler başlatılıyor…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form), signal: AbortSignal.timeout(30000)});
      const data = await response.json();
      if (!response.ok || !data.ok) {
        const cameras = (data.camera_groups || []).map(group => group.join(', ')).join(' / ');
        finish((data.message || 'İstek kabul edilmedi. Sistem durumunu kontrol edin.') + (cameras ? ' Kameralar: ' + cameras : ''));
      } else if (pending) pending.revision = data.desired_camera_revision;
      clearTimeout(timer); refresh();
    } catch (_) {finish('İşlem sonucu alınamadı. Tekrar denemeden önce durumu kontrol edin.');}
  }));
  document.querySelectorAll('[data-evidence]').forEach(media => {
    const unavailable = () => {media.hidden = true; media.nextElementSibling.hidden = false;};
    media.addEventListener('error', unavailable);
    if (media.error || (media.tagName === 'IMG' && media.complete && !media.naturalWidth)) unavailable();
  });
  refresh();
})();
