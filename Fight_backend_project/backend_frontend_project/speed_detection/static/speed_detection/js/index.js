let speedTable = null;
const speedRowMap = new Map();

const speedPage = document.getElementById("speed-page");
const speedStatusUrl = speedPage?.dataset.statusUrl || "";

let fullscreenCameraCard = null;
let fullscreenBackdrop = null;

const datatableLang = {
  decimal: "",
  emptyTable: "Kayıt bulunamadı",
  info: "_TOTAL_ kaydın _START_ - _END_ arası gösteriliyor",
  infoEmpty: "0 kayıt gösteriliyor",
  infoFiltered: "(_MAX_ kayıt içinden filtrelendi)",
  lengthMenu: "Sayfada _MENU_ kayıt göster",
  loadingRecords: "Yükleniyor...",
  processing: "İşleniyor...",
  search: "Filtrele:",
  zeroRecords: "Eşleşen kayıt bulunamadı",
  paginate: {
    first: "İlk",
    last: "Son",
    next: "Sonraki",
    previous: "Önceki",
  },
};

function escapeHtml(value) {
  if (value === null || value === undefined || value === "") return "-";

  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function rawValue(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function eventKey(row) {
  return `${row.run_name || "-"}__${row.camera_id || "-"}__${row.track_id || "-"}__${row.frame_idx || "-"}`;
}

function initTable() {
  const tableEl = document.getElementById("speed-events-table");

  if (!tableEl) return;

  speedTable = $("#speed-events-table").DataTable({
    pageLength: 5,
    lengthMenu: [
      [5, 10, 25, 50, 100],
      [5, 10, 25, 50, 100],
    ],
    order: [[5, "desc"]],
    language: datatableLang,
    responsive: false,
    autoWidth: false,
    columnDefs: [{ orderable: false, targets: [6, 7] }],
  });

  rebuildMap();
}

function rebuildMap() {
  speedRowMap.clear();

  if (!speedTable) return;

  speedTable.rows().every(function () {
    const node = this.node();
    const key = node?.getAttribute("data-row-key");

    if (key) {
      speedRowMap.set(key, this);
    }
  });
}

function ensureCameraFullscreenBackdrop() {
  if (fullscreenBackdrop) return fullscreenBackdrop;

  const backdrop = document.createElement("button");
  backdrop.type = "button";
  backdrop.className = "speed-camera-fullscreen-backdrop";
  backdrop.setAttribute("aria-label", "Tam ekran kamerayı kapat");

  document.body.appendChild(backdrop);

  backdrop.addEventListener("click", function () {
    closeFullscreenCamera();
  });

  fullscreenBackdrop = backdrop;
  return fullscreenBackdrop;
}

function ensureCameraFullscreenCloseButton(card) {
  let button = card.querySelector("[data-speed-fullscreen-close]");

  if (button) return button;

  const head = card.querySelector(".speed-camera-card__head") || card;

  button = document.createElement("button");
  button.type = "button";
  button.className = "speed-camera-fullscreen-close";
  button.setAttribute("data-speed-fullscreen-close", "1");
  button.setAttribute("aria-label", "Kamerayı küçült");
  button.innerHTML = `
    <span aria-hidden="true">×</span>
    <span>Küçült</span>
  `;

  head.appendChild(button);

  return button;
}

function openFullscreenCamera(card) {
  if (!card) return;

  const preview = card.querySelector("[data-speed-preview]");

  /*
    Boş preview alanını büyütmeye gerek yok.
    İçinde img varsa canlı stream veya preview var demektir.
  */
  if (!preview || !preview.querySelector("img")) return;

  if (fullscreenCameraCard && fullscreenCameraCard !== card) {
    closeFullscreenCamera();
  }

  fullscreenCameraCard = card;

  const backdrop = ensureCameraFullscreenBackdrop();

  ensureCameraFullscreenCloseButton(card);

  card.classList.add("is-fullscreen");
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");

  backdrop.classList.add("is-open");
  document.body.classList.add("speed-camera-fullscreen-open");
}

function closeFullscreenCamera() {
  if (!fullscreenCameraCard) return;

  const closeButton = fullscreenCameraCard.querySelector("[data-speed-fullscreen-close]");

  fullscreenCameraCard.classList.remove("is-fullscreen");
  fullscreenCameraCard.removeAttribute("role");
  fullscreenCameraCard.removeAttribute("aria-modal");

  if (closeButton) {
    closeButton.remove();
  }

  fullscreenCameraCard = null;

  if (fullscreenBackdrop) {
    fullscreenBackdrop.classList.remove("is-open");
  }

  document.body.classList.remove("speed-camera-fullscreen-open");
}

function initCameraFullscreenViewer() {
  document.addEventListener("click", function (event) {
    const closeButton = event.target.closest("[data-speed-fullscreen-close]");

    if (closeButton) {
      event.preventDefault();
      event.stopPropagation();
      closeFullscreenCamera();
      return;
    }

    const preview = event.target.closest("[data-speed-preview]");

    if (!preview) return;

    const card = preview.closest(".speed-camera-card");

    if (!card) return;

    if (card.classList.contains("is-fullscreen")) return;

    openFullscreenCamera(card);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeFullscreenCamera();
      return;
    }

    if (event.key !== "Enter" && event.key !== " ") return;

    const activePreview = document.activeElement?.closest?.("[data-speed-preview]");

    if (!activePreview) return;

    event.preventDefault();

    const card = activePreview.closest(".speed-camera-card");

    openFullscreenCamera(card);
  });
}

function updateStatusBadge(running) {
  const el = document.getElementById("speed-live-status-badge");

  if (!el) return;

  el.innerHTML = running
    ? `
      <span class="system-status__badge system-status__badge--on">
        <span class="system-status__dot"></span>
        Hız Tespiti Aktif
      </span>
    `
    : `
      <span class="system-status__badge system-status__badge--off">
        <span class="system-status__dot"></span>
        Hız Tespiti Durdu
      </span>
    `;
}

function updateSummary(data) {
  const el = document.getElementById("speed-summary");

  if (!el) return;

  el.innerHTML = `
    <div class="speed-summary__item">
      <span>Sistem Durumu</span>
      <strong>${data.running ? "Çalışıyor" : "Duruyor"}</strong>
    </div>

    <div class="speed-summary__item">
      <span>Kamera</span>
      <strong>${escapeHtml(data.camera_count ?? 0)}</strong>
    </div>

    <div class="speed-summary__item">
      <span>İhlal</span>
      <strong>${escapeHtml((data.events || []).length)}</strong>
    </div>
  `;
}

function updateRuntimeError(data) {
  const el = document.getElementById("speed-runtime-error");

  if (!el) return;

  const text = data.last_error || data.message || "";

  if (!text) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }

  el.style.display = "block";
  el.textContent = text;
}

function statusBadgeHtml(running) {
  return running
    ? `<span class="speed-badge speed-badge--on" data-speed-card-status>Aktif</span>`
    : `<span class="speed-badge speed-badge--off" data-speed-card-status>Durdu</span>`;
}

function cameraCalibrationHtml(camera) {
  const ready = Boolean(camera.calibration_ready);
  const reason = camera.calibration_reason || "Kalibrasyon tamamlanmamış.";
  const adminUrl = rawValue(camera.admin_url || "");

  return `
    <div class="speed-camera-calibration" data-speed-calibration>
      ${adminUrl ? `<a href="${escapeHtml(adminUrl)}" class="speed-action-btn speed-action-btn--settings">Admin Ayarları</a>` : ""}
      ${
        ready
          ? `<span class="speed-badge speed-badge--on">Kalibre</span>`
          : `
            <span class="speed-badge speed-badge--warn">Kalibrasyon Gerekli</span>
            <small>${escapeHtml(reason)}</small>
          `
      }
    </div>
  `;
}

function cameraPreviewHtml(camera) {
  const streamUrl = rawValue(camera.stream_url || camera.raw_stream_url || "");
  const previewUrl = rawValue(camera.preview_url || "");

  if (streamUrl) {
    return `<img class="speed-live-stream" src="${escapeHtml(streamUrl)}" alt="${escapeHtml(camera.camera_id)} canlı kamera" />`;
  }

  if (previewUrl) {
    return `<img src="${escapeHtml(previewUrl)}?t=${Date.now()}" alt="${escapeHtml(camera.camera_id)} preview" />`;
  }

  return `<span>Canlı akış bekleniyor</span>`;
}

function cameraCardHtml(camera, running) {
  return `
    <article class="speed-camera-card" data-camera-id="${escapeHtml(camera.camera_id)}">
      <div class="speed-camera-card__head">
        <div>
          <strong data-speed-camera-name>${escapeHtml(camera.name)}</strong>
          <small data-speed-camera-id>${escapeHtml(camera.camera_id)}</small>
        </div>

        ${statusBadgeHtml(running)}
      </div>

      <div
        class="speed-preview"
        data-speed-preview
        role="button"
        tabindex="0"
        title="Kamerayı büyüt"
      >
        ${cameraPreviewHtml(camera)}
      </div>

      <div class="speed-camera-meta">
        <div>
          <span>Limit</span>
          <strong data-speed-meta="limit">${escapeHtml(camera.speed_limit_kmh)} km/h</strong>
        </div>

        <div>
          <span>Tolerans</span>
          <strong data-speed-meta="tolerance">${escapeHtml(camera.tolerance_kmh)}</strong>
        </div>

        <div>
          <span>Track</span>
          <strong data-speed-meta="tracks">${escapeHtml(camera.tracks ?? 0)}</strong>
        </div>

        <div>
          <span>Son Hız</span>
          <strong data-speed-meta="latest_speed">${escapeHtml(camera.latest_speed_kmh)}</strong>
        </div>
      </div>

      ${cameraCalibrationHtml(camera)}
    </article>
  `;
}

function updateCameraCardFields(card, camera, running) {
  const badge = card.querySelector("[data-speed-card-status]");

  if (badge) {
    badge.outerHTML = statusBadgeHtml(running);
  }

  const nameEl = card.querySelector("[data-speed-camera-name]");

  if (nameEl) {
    nameEl.textContent = rawValue(camera.name || "-");
  }

  const idEl = card.querySelector("[data-speed-camera-id]");

  if (idEl) {
    idEl.textContent = rawValue(camera.camera_id || "-");
  }

  const limit = card.querySelector('[data-speed-meta="limit"]');

  if (limit) {
    limit.textContent = `${rawValue(camera.speed_limit_kmh || "-")} km/h`;
  }

  const tolerance = card.querySelector('[data-speed-meta="tolerance"]');

  if (tolerance) {
    tolerance.textContent = rawValue(camera.tolerance_kmh || "-");
  }

  const tracks = card.querySelector('[data-speed-meta="tracks"]');

  if (tracks) {
    tracks.textContent = rawValue(camera.tracks ?? 0);
  }

  const latest = card.querySelector('[data-speed-meta="latest_speed"]');

  if (latest) {
    latest.textContent = rawValue(camera.latest_speed_kmh || "-");
  }

  const calibration = card.querySelector("[data-speed-calibration]");

  if (calibration) {
    calibration.outerHTML = cameraCalibrationHtml(camera);
  }

  /*
    Canlı stream img'sine normalde dokunmuyoruz.
    Çünkü src değişirse MJPEG bağlantısı kopar ve görüntü tekrar başlar.
    Sadece ilk yüklemede stream yoksa ve sonradan stream_url geldiyse preview alanını dolduruyoruz.
  */
  const previewShell = card.querySelector("[data-speed-preview]");
  const liveImg = previewShell ? previewShell.querySelector(".speed-live-stream") : null;
  const hasAnyImg = previewShell ? previewShell.querySelector("img") : null;
  const streamUrl = rawValue(camera.stream_url || camera.raw_stream_url || "");

  if (previewShell && !liveImg && !hasAnyImg && streamUrl) {
    previewShell.innerHTML = cameraPreviewHtml(camera);
  }
}

function updateCameras(cameras, running) {
  const grid = document.getElementById("speed-camera-grid");

  if (!grid) return;

  if (!cameras || cameras.length === 0) {
    closeFullscreenCamera();
    grid.innerHTML = `<div class="empty-state">Hız tespiti için aktif kamera bulunmuyor.</div>`;
    return;
  }

  const currentCards = Array.from(grid.querySelectorAll(".speed-camera-card"));

  const currentIds = currentCards
    .map((card) => rawValue(card.getAttribute("data-camera-id")))
    .join("|");

  const nextIds = cameras
    .map((camera) => rawValue(camera.camera_id))
    .join("|");

  /*
    Kamera listesi değişmediyse kartı komple yeniden çizme.
    Böylece canlı kamera bağlantısı polling sırasında kopmaz.
  */
  if (currentCards.length !== cameras.length || currentIds !== nextIds) {
    closeFullscreenCamera();
    grid.innerHTML = cameras.map((camera) => cameraCardHtml(camera, running)).join("");
    return;
  }

  const cardById = new Map(
    currentCards.map((card) => [
      rawValue(card.getAttribute("data-camera-id")),
      card,
    ]),
  );

  cameras.forEach((camera) => {
    const card = cardById.get(rawValue(camera.camera_id));

    if (!card) return;

    updateCameraCardFields(card, camera, running);
  });
}

function syncEvents(rows) {
  if (!speedTable) return;

  let changed = false;

  (rows || []).forEach((row) => {
    const key = eventKey(row);

    const snapshotHtml = row.snapshot_url
      ? `
        <a href="${escapeHtml(row.snapshot_url)}" target="_blank" rel="noopener">
          <img class="speed-snapshot" src="${escapeHtml(row.snapshot_url)}" alt="snapshot" />
        </a>
      `
      : "-";

    const clipHtml = row.clip_url
      ? `
        <video class="speed-video" controls preload="metadata">
          <source src="${escapeHtml(row.clip_url)}" type="video/mp4" />
        </video>
      `
      : "-";

    const cells = [
      escapeHtml(row.camera_id),
      `${escapeHtml(row.vehicle_class)} / #${escapeHtml(row.track_id)}`,
      `<span class="speed-badge speed-badge--warn">${escapeHtml(row.speed_kmh)} km/h</span>`,
      `${escapeHtml(row.speed_limit_kmh)} + ${escapeHtml(row.tolerance_kmh)}`,
      escapeHtml(row.frame_idx),
      escapeHtml(row.created_at_text),
      snapshotHtml,
      clipHtml,
    ];

    if (speedRowMap.has(key)) {
      const rowApi = speedRowMap.get(key);
      rowApi.data(cells);
    } else {
      const rowApi = speedTable.row.add(cells);
      speedTable.draw(false);

      const node = rowApi.node();

      if (node) {
        node.setAttribute("data-row-key", key);
      }
    }

    changed = true;
  });

  if (changed) {
    speedTable.draw(false);
    rebuildMap();
  }
}

async function pollSpeed() {
  try {
    if (!speedStatusUrl) {
      console.error("Speed status URL bulunamadı. index.html içindeki data-status-url kontrol edilmeli.");
      return;
    }

    const response = await fetch(speedStatusUrl, {
      method: "GET",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
      },
      cache: "no-store",
    });

    if (!response.ok) return;

    const data = await response.json();

    if (!data.ok) return;

    updateStatusBadge(data.running);
    updateSummary(data);
    updateCameras(data.cameras || [], data.running);
    updateRuntimeError(data);
    syncEvents(data.events || []);
  } catch (err) {
    console.error("Speed status polling hatası:", err);
  }
}

async function submitControlForm(form) {
  const button = form.querySelector("button[type='submit']");

  try {
    if (button) button.disabled = true;

    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: {
        "X-Requested-With": "XMLHttpRequest",
      },
      cache: "no-store",
    });

    const data = await response.json().catch(() => null);

    if (data) {
      updateStatusBadge(data.running);
      updateRuntimeError(data);
    }

    await pollSpeed();

    if (!response.ok && data?.message) {
      alert(data.message);
    }
  } catch (err) {
    console.error("Hız tespiti form hatası:", err);
    alert("İşlem sırasında hata oluştu. Konsolu ve Django terminalini kontrol et.");
  } finally {
    if (button) button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", function () {
  initTable();
  initCameraFullscreenViewer();

  const errorEl = document.getElementById("speed-runtime-error");

  if (errorEl && errorEl.textContent.trim()) {
    errorEl.style.display = "block";
  }

  document.querySelectorAll(".speed-control-form").forEach((form) => {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitControlForm(form);
    });
  });

  pollSpeed();
  setInterval(pollSpeed, 3000);
});