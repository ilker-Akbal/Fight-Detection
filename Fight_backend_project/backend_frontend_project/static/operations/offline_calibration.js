/* Historical playback only. No camera/preview URL or live capture is opened. */
(() => {
  const canvas = document.querySelector('[data-offline-calibration]');
  if (!canvas) return;
  const video = document.querySelector('[data-offline-video]');
  const context = canvas.getContext('2d');
  const output = document.getElementById('id_calibration');
  const status = document.querySelector('[data-calibration-status]');
  let base = null, points = [];
  const draw = () => {
    if (!base) return;
    context.putImageData(base, 0, 0);
    context.lineWidth = 3;
    points.forEach(([x, y], index) => {
      context.strokeStyle = index < 2 ? '#00ff80' : '#ffbf00';
      context.beginPath(); context.arc(x, y, 5, 0, Math.PI * 2); context.stroke();
      if (index % 2) {
        context.beginPath(); context.moveTo(...points[index - 1]); context.lineTo(x, y); context.stroke();
      }
    });
  };
  document.querySelector('[data-calibration-frame]').addEventListener('click', () => {
    if (!video.videoWidth || video.readyState < 2) {status.textContent = 'Önce videoyu yükleyin ve bir kareye ilerleyin.'; return;}
    video.pause();
    canvas.width = Number(canvas.dataset.width) || video.videoWidth;
    canvas.height = Math.floor(video.videoHeight * canvas.width / video.videoWidth);
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    base = context.getImageData(0, 0, canvas.width, canvas.height);
    points = []; canvas.hidden = false;
    status.textContent = 'Önce A çizgisinin, ardından B çizgisinin iki ucunu seçin (4 tıklama).';
  });
  canvas.addEventListener('click', event => {
    if (!base || points.length >= 4) return;
    const bounds = canvas.getBoundingClientRect();
    points.push([Math.round((event.clientX - bounds.left) * canvas.width / bounds.width),
                 Math.round((event.clientY - bounds.top) * canvas.height / bounds.height)]);
    draw();
  });
  document.querySelector('[data-calibration-apply]').addEventListener('click', () => {
    const distance = Number(document.getElementById('offline-distance').value);
    const limit = Number(document.getElementById('offline-limit').value);
    const tolerance = Number(document.getElementById('offline-tolerance').value);
    if (points.length !== 4 || !Number.isFinite(distance) || distance <= 0 || !Number.isFinite(limit) || limit <= 0 || !Number.isFinite(tolerance) || tolerance < 0) {
      status.textContent = 'İki çizgi, pozitif mesafe/hız limiti ve geçerli tolerans gereklidir.'; return;
    }
    output.value = JSON.stringify({camera_id: 'offline', speed_limit_kmh: limit, tolerance_kmh: tolerance,
      measurement: {mode: 'two_line_time_gate', direction: document.getElementById('offline-direction').value,
                    line_a: points.slice(0, 2), line_b: points.slice(2), distance_m: distance},
      road_roi: {enabled: false, polygon: []},
      meta: {frame_coordinate_space: 'pipeline_resize_width', resize_width: Number(canvas.dataset.width),
             frame_width: canvas.width, frame_height: canvas.height}}, null, 2);
    status.textContent = 'Kalibrasyon belgeye aktarıldı. Analizi Kuyruğa Al ile yeni çalışma başlatın.';
  });
})();
