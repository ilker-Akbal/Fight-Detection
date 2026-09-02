let incidentsTable = null;
const incidentRowMap = new Map();

const dashboardRoot = document.getElementById("dashboard-overview");

const dashboardConfig = {
  eventsUrl: dashboardRoot?.dataset.eventsUrl || "",
  eventsStreamUrl: dashboardRoot?.dataset.eventsStreamUrl || "",
  incidentVideoTemplate: dashboardRoot?.dataset.incidentVideoTemplate || "",
  logoUrl: dashboardRoot?.dataset.logoUrl || "",
  csrfToken: dashboardRoot?.dataset.csrfToken || "",
};

const shownFightAlerts = new Set(
  JSON.parse(sessionStorage.getItem("shownFightAlerts") || "[]"),
);

const datatableLang = {
  decimal: "",
  emptyTable: "Kayıt bulunamadı",
  info: "_TOTAL_ kaydın _START_ - _END_ arası gösteriliyor",
  infoEmpty: "0 kayıt gösteriliyor",
  infoFiltered: "(_MAX_ kayıt içinden filtrelendi)",
  infoPostFix: "",
  thousands: ".",
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
  if (value === null || value === undefined) return "-";

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

function clipBaseName(value) {
  if (!value) return "";

  return (
    String(value)
      .replaceAll("\\", "/")
      .split("/")
      .filter(Boolean)
      .pop() || ""
  );
}

function buildIncidentUrl(runName, clipPathOrName) {
  if (!runName || !clipPathOrName) return "";

  const clipName = clipBaseName(clipPathOrName);

  if (!clipName || !dashboardConfig.incidentVideoTemplate) return "";

  return dashboardConfig.incidentVideoTemplate
    .replace("__RUN__", encodeURIComponent(runName))
    .replace("__CLIP__", encodeURIComponent(clipName));
}

function statusBadge(statusText) {
  const raw = String(statusText || "-");
  const normalized = raw.toLowerCase();

  let cls = "status-other";

  if (normalized.includes("non") || normalized.includes("normal")) {
    cls = "status-nonfight";
  } else if (normalized.includes("fight")) {
    cls = "status-fight";
  }

  return `<span class="status-badge ${cls}">${escapeHtml(raw)}</span>`;
}

function incidentKey(row) {
  const clipName = clipBaseName(row.clip_name || row.clip_path || "-");
  return `${row.run_name || "-"}__${row.camera_id || "-"}__${row.incident_id || "-"}__${clipName || "-"}`;
}

function fightAlertKey(row) {
  const clipName = clipBaseName(row.clip_name || row.clip_path || "-");
  return `${row.run_name || "-"}__${row.camera_id || "-"}__${row.incident_id || "-"}__${clipName || "-"}`;
}

function isFightIncident(row) {
  const label = String(row.final_label || row.fight_label || "").toLowerCase();
  return label.includes("fight") && !label.includes("non");
}

function saveShownFightAlerts() {
  sessionStorage.setItem(
    "shownFightAlerts",
    JSON.stringify(Array.from(shownFightAlerts).slice(-80)),
  );
}

function checkFightAlerts(rows) {
  (rows || []).forEach(function (row) {
    if (!isFightIncident(row)) return;

    const key = fightAlertKey(row);

    if (shownFightAlerts.has(key)) return;

    shownFightAlerts.add(key);
    saveShownFightAlerts();

    showFightAlert(row);
  });
}

function showFightAlert(row) {
  const oldModal = document.getElementById("fight-alert-modal");

  if (oldModal) {
    oldModal.remove();
  }

  const clipUrl = buildIncidentUrl(row.run_name, row.clip_name || row.clip_path);

  const modal = document.createElement("div");
  modal.id = "fight-alert-modal";
  modal.className = "fight-alert-modal";

  modal.innerHTML = `
    <div class="fight-alert-modal__backdrop" data-fight-alert-close></div>

    <div class="fight-alert-modal__box" role="dialog" aria-modal="true">
      <div class="fight-alert-modal__icon">!</div>

      <div class="fight-alert-modal__content">
        <span class="fight-alert-modal__eyebrow">Acil Bildirim</span>
        <h2>Kavga Tespit Edildi</h2>

        <p>
          <strong>${escapeHtml(row.camera_id || "Kamera")}</strong>
          kamerasında kavga olayı tespit edildi.
        </p>

        <div class="fight-alert-modal__details">
          <div>
            <span>Başlangıç</span>
            <strong>${escapeHtml(row.start_ts || "-")}</strong>
          </div>

          <div>
            <span>Bitiş</span>
            <strong>${escapeHtml(row.end_ts || "-")}</strong>
          </div>

          <div>
            <span>Etiket</span>
            <strong>${escapeHtml(row.final_label || "fight")}</strong>
          </div>
        </div>

        <div class="fight-alert-modal__actions">
          ${
            clipUrl
              ? `<a class="fight-alert-modal__primary" href="${clipUrl}" target="_blank" rel="noopener">Kaydı Aç</a>`
              : ""
          }

          <button type="button" class="fight-alert-modal__secondary" data-fight-alert-close>
            Tamam
          </button>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(modal);
  document.body.classList.add("fight-alert-modal-open");

  function closeModal() {
    modal.classList.add("is-closing");

    setTimeout(function () {
      modal.remove();
      document.body.classList.remove("fight-alert-modal-open");
    }, 180);
  }

  modal.querySelectorAll("[data-fight-alert-close]").forEach(function (el) {
    el.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", function escHandler(event) {
    if (event.key === "Escape") {
      closeModal();
      document.removeEventListener("keydown", escHandler);
    }
  });
}

function initTables() {
  const incidentsEl = document.getElementById("incidents-table");

  if (incidentsEl) {
    incidentsTable = $("#incidents-table").DataTable({
      pageLength: 5,
      lengthMenu: [
        [5, 10, 25, 50, 100],
        [5, 10, 25, 50, 100],
      ],
      order: [[2, "desc"]],
      language: datatableLang,
      responsive: false,
      autoWidth: false,
      columnDefs: [{ orderable: false, targets: [5] }],
    });
  }

  rebuildMaps();
}

function rebuildMaps() {
  incidentRowMap.clear();

  if (incidentsTable) {
    incidentsTable.rows().every(function () {
      const node = this.node();
      const key = node?.getAttribute("data-row-key");

      if (key) {
        incidentRowMap.set(key, this);
      }
    });
  }
}

function updateLiveStatusBadge(running) {
  const el = document.getElementById("live-status-badge");

  if (!el) return;

  if (running) {
    el.innerHTML = `
      <span class="system-status__badge system-status__badge--on">
        <span class="system-status__dot"></span>
        Sistem Aktif
      </span>
    `;
  } else {
    el.innerHTML = `
      <span class="system-status__badge system-status__badge--off">
        <span class="system-status__dot"></span>
        Sistem Durduruldu
      </span>
    `;
  }
}

function updatePipelineSummary(data) {
  const el = document.getElementById("pipeline-summary");

  if (!el) return;

  const running = !!data.running;

  el.innerHTML = `
    <div class="summary-card">
      <span>Sistem Durumu</span>
      <strong>${running ? "Çalışıyor" : "Duruyor"}</strong>
    </div>

    <div class="summary-card">
      <span>Aktif Kamera Sayısı</span>
      <strong>${escapeHtml(data.camera_count ?? 0)}</strong>
    </div>
  `;
}

function syncIncidents(rows) {
  if (!incidentsTable) return;

  (rows || []).forEach((row) => {
    const key = incidentKey(row);
    const clipUrl = buildIncidentUrl(row.run_name, row.clip_name || row.clip_path);

    const clipHtml = clipUrl
      ? `
        <div class="incident-video-wrap">
          <video class="incident-video" controls preload="metadata">
            <source src="${clipUrl}" type="video/mp4" />
          </video>
        </div>
      `
      : `<span class="incident-empty">Clip yok</span>`;

    const cells = [
      escapeHtml(row.camera_id),
      escapeHtml(row.incident_id),
      escapeHtml(row.start_ts),
      escapeHtml(row.end_ts),
      statusBadge(row.final_label),
      clipHtml,
    ];

    if (!incidentRowMap.has(key)) {
      const rowApi = incidentsTable.row.add(cells);
      incidentsTable.draw(false);

      const node = rowApi.node();

      if (node) {
        node.setAttribute("data-row-key", key);
      }

      rebuildMaps();
      return;
    }

    const rowApi = incidentRowMap.get(key);
    const node = rowApi.node();

    if (!node) return;

    const tds = node.querySelectorAll("td");

    if (tds.length >= 6) {
      tds[0].textContent = row.camera_id ?? "-";
      tds[1].textContent = row.incident_id ?? "-";
      tds[2].textContent = row.start_ts ?? "-";
      tds[3].textContent = row.end_ts ?? "-";
      tds[4].innerHTML = cells[4];
      tds[5].innerHTML = clipHtml;
    }
  });
}

function syncOperationalInbox(rows) {
  const body = document.getElementById("operational-inbox-body");
  if (!body) return;

  if (!rows || rows.length === 0) {
    body.innerHTML = `<tr><td colspan="8" class="muted">Aktif route edilmiş olay bulunmuyor.</td></tr>`;
    return;
  }

  body.innerHTML = rows.map((item) => {
    let actionHtml = "-";
    if (item.can_ack) {
      actionHtml = `
        <form method="post" action="${escapeHtml(item.ack_url)}">
          <input type="hidden" name="csrfmiddlewaretoken" value="${escapeHtml(dashboardConfig.csrfToken)}" />
          <input type="hidden" name="security_unit_id" value="${escapeHtml(item.security_unit_id)}" />
          <button type="submit" class="btn">Kabul Et</button>
        </form>`;
    } else if (item.can_resolve) {
      actionHtml = `
        <form method="post" action="${escapeHtml(item.resolve_url)}">
          <input type="hidden" name="csrfmiddlewaretoken" value="${escapeHtml(dashboardConfig.csrfToken)}" />
          <input type="text" name="resolution_note" maxlength="4000" placeholder="Çözüm notu" required />
          <button type="submit" class="btn">Çözümle</button>
        </form>`;
    }

    const evidenceHtml = item.evidence_url
      ? `<a href="${escapeHtml(item.evidence_url)}" target="_blank" rel="noopener">Kanıtı Aç</a>`
      : "-";

    return `
      <tr data-operational-route-id="${escapeHtml(item.route_id)}">
        <td><strong>${escapeHtml(item.incident_type)} #${escapeHtml(item.external_incident_id)}</strong><br />
          <small>${escapeHtml(item.camera_name)} / ${escapeHtml(item.camera_id)}</small></td>
        <td>${escapeHtml(item.location)}</td>
        <td>${escapeHtml(item.security_unit)}</td>
        <td>${escapeHtml(item.routing_stage)}</td>
        <td><span class="status-badge status-other">${escapeHtml(item.status)}</span><br />
          <small>${escapeHtml(item.acknowledged_by || "")}</small></td>
        <td>${escapeHtml(Number(item.decision_score || 0).toFixed(3))}</td>
        <td>${evidenceHtml}</td>
        <td>${actionHtml}</td>
      </tr>`;
  }).join("");
}

let lastDashboardNoticeId = sessionStorage.getItem("lastDashboardNoticeId") || "";

function showDashboardNoticeModal(notice) {
  if (!notice || !notice.id) return;

  const oldModal = document.getElementById("dashboard-notice-modal");

  if (oldModal) {
    oldModal.remove();
  }

  const allowedKinds = new Set(["success", "warning", "error", "info"]);
  const rawKind = rawValue(notice.kind || "info");
  const kind = allowedKinds.has(rawKind) ? rawKind : "info";
  const title = notice.title || "Sistem Bildirimi";
  const message = notice.message || "Sistem durumu güncellendi.";
  const logoUrl = dashboardConfig.logoUrl || "";

  const modal = document.createElement("div");
  modal.id = "dashboard-notice-modal";
  modal.className = `dashboard-notice-modal dashboard-notice-modal--${kind}`;

  modal.innerHTML = `
    <div class="dashboard-notice-modal__backdrop" data-dashboard-notice-close></div>

    <div class="dashboard-notice-modal__box" role="dialog" aria-modal="true">
      <div class="dashboard-notice-modal__logo">
        <img src="${escapeHtml(logoUrl)}" alt="TOGÜ Logo" />
      </div>

      <div class="dashboard-notice-modal__icon"></div>

      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(message)}</p>

      <button
        type="button"
        class="dashboard-notice-modal__close"
        data-dashboard-notice-close
      >
        Tamam
      </button>
    </div>
  `;

  document.body.appendChild(modal);
  document.body.classList.add("dashboard-notice-modal-open");

  function closeModal() {
    modal.classList.add("is-closing");

    setTimeout(function () {
      modal.remove();
      document.body.classList.remove("dashboard-notice-modal-open");
    }, 220);
  }

  modal.querySelectorAll("[data-dashboard-notice-close]").forEach(function (el) {
    el.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", function escHandler(event) {
    if (event.key === "Escape") {
      closeModal();
      document.removeEventListener("keydown", escHandler);
    }
  });
}

function handleDashboardNotice(notice) {
  if (!notice || !notice.id) return;

  if (notice.id === lastDashboardNoticeId) return;

  lastDashboardNoticeId = notice.id;
  sessionStorage.setItem("lastDashboardNoticeId", notice.id);

  showDashboardNoticeModal(notice);
}

function applyDashboardData(data) {
  handleDashboardNotice(data.notice);

  updateLiveStatusBadge(data.running);

  updatePipelineSummary({
    running: data.running,
    camera_count: data.camera_count,
  });

  syncIncidents(data.incidents || []);
  syncOperationalInbox(data.operational_incidents || []);
  checkFightAlerts(data.incidents || []);
}

function initCameraModal() {
  const modal = document.getElementById("camera-modal");
  const modalImg = document.getElementById("camera-modal-img");
  const modalTitle = document.getElementById("camera-modal-title");
  const modalSubtitle = document.getElementById("camera-modal-subtitle");

  if (!modal || !modalImg || !modalTitle || !modalSubtitle) return;

  function openModal(button) {
    const card = button.closest(".camera-card");
    const img = button.querySelector("img");

    if (!card || !img) return;

    const cameraName = button.dataset.cameraName || card.dataset.cameraName || "Kamera";
    const cameraId = button.dataset.cameraId || card.dataset.cameraId || "-";

    modalTitle.textContent = cameraName;
    modalSubtitle.textContent = `Kamera ID: ${cameraId}`;
    modalImg.src = img.src;

    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("camera-modal-open");
  }

  function closeModal() {
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("camera-modal-open");
    modalImg.src = "";
  }

  document.querySelectorAll(".camera-open-btn").forEach((button) => {
    button.addEventListener("click", function () {
      openModal(button);
    });
  });

  document.querySelectorAll("[data-camera-modal-close]").forEach((button) => {
    button.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal.classList.contains("is-open")) {
      closeModal();
    }
  });
}

function connectSSE() {
  if (!dashboardConfig.eventsStreamUrl) {
    startPollingFallback();
    return;
  }

  const source = new EventSource(dashboardConfig.eventsStreamUrl);

  source.addEventListener("dashboard", function (event) {
    try {
      const data = JSON.parse(event.data);

      if (!data.ok) return;

      applyDashboardData(data);
    } catch (err) {
      console.error("SSE parse hatası:", err);
    }
  });

  source.addEventListener("ping", function () {});

  source.onerror = function (err) {
    console.error("SSE bağlantı hatası:", err);
    source.close();
    startPollingFallback();
  };
}

let pollingStarted = false;

function startPollingFallback() {
  if (pollingStarted) return;

  pollingStarted = true;

  async function tick() {
    try {
      if (!dashboardConfig.eventsUrl) return;

      const response = await fetch(dashboardConfig.eventsUrl, {
        method: "GET",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
        },
        cache: "no-store",
      });

      if (!response.ok) return;

      const data = await response.json();

      if (!data.ok) return;

      applyDashboardData(data);
    } catch (err) {
      console.error("Fallback polling hatası:", err);
    }
  }

  tick();
  setInterval(tick, 5000);
}

document.addEventListener("DOMContentLoaded", function () {
  initTables();
  initCameraModal();
  connectSSE();
});
