const cameraRows = document.getElementById("cameraRows");
const cameraCards = document.getElementById("cameraCards");
const addRowBtn = document.getElementById("addRowBtn");
const clearRowsBtn = document.getElementById("clearRowsBtn");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const rowTemplate = document.getElementById("cameraRowTemplate");

function getCookie(name) {
  let cookieValue = null;

  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");

    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();

      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }

  return cookieValue;
}

const csrfToken = getCookie("fight_csrftoken") || getCookie("csrftoken");

function updateLiveStatusBadge(pipeline) {
  const badge = document.getElementById("live-status-badge");
  if (!badge) return;

  const running = !!pipeline?.running;

  badge.innerHTML = running
    ? '<span class="status-on">● Sistem Aktif</span>'
    : '<span class="status-off">● Sistem Durduruldu</span>';
}

function createRow(cameraId = "", source = "") {
  if (!rowTemplate || !cameraRows) return;

  const node = rowTemplate.content.cloneNode(true);
  const row = node.querySelector(".camera-row");
  const title = node.querySelector(".row-title");
  const idInput = node.querySelector(".camera-id-input");
  const sourceInput = node.querySelector(".camera-source-input");
  const removeBtn = node.querySelector(".remove-row-btn");

  idInput.value = cameraId;
  sourceInput.value = source;

  function refreshTitle() {
    const value = idInput.value.trim();
    title.textContent = value ? value : "Kamera";
  }

  idInput.addEventListener("input", refreshTitle);
  refreshTitle();

  removeBtn.addEventListener("click", () => {
    row.remove();

    if (!document.querySelector(".camera-row")) {
      createRow();
    }
  });

  cameraRows.appendChild(node);
}

function collectSources() {
  const rows = [...document.querySelectorAll(".camera-row")];

  return rows
    .map((row) => {
      const cameraId = row.querySelector(".camera-id-input").value.trim();
      const source = row.querySelector(".camera-source-input").value.trim();

      return {
        camera_id: cameraId,
        source: source,
      };
    })
    .filter((item) => item.camera_id && item.source);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 3) {
  if (value === "" || value === null || value === undefined) {
    return "-";
  }

  const num = Number(value);

  if (Number.isNaN(num)) {
    return escapeHtml(value);
  }

  return num.toFixed(digits);
}

function buildStatusBadge(camera) {
  const label =
    camera.latest_stage3_label ||
    camera.latest_event_status ||
    camera.detail ||
    "beklemede";

  const detail = String(label).toLowerCase();

  let cls = "camera-badge camera-badge--idle";

  if (detail.includes("fight")) {
    cls = "camera-badge camera-badge--danger";
  } else if (
    detail.includes("queued") ||
    detail.includes("processing") ||
    detail.includes("event") ||
    detail.includes("tick") ||
    detail.includes("started")
  ) {
    cls = "camera-badge camera-badge--warning";
  } else if (
    detail.includes("completed") ||
    detail.includes("processed") ||
    detail.includes("non_fight") ||
    detail.includes("inactive")
  ) {
    cls = "camera-badge camera-badge--ok";
  }

  return `<span class="${cls}">${escapeHtml(label)}</span>`;
}

function buildMetaBox(label, value) {
  return `<div><b>${escapeHtml(label)}:</b> ${escapeHtml(value)}</div>`;
}

function buildCard(camera) {
  const cameraId = camera.camera_id || "";
  const source = camera.source || "";
  const stage = camera.stage || "-";
  const detail = camera.detail || "-";
  const persons = camera.persons ?? "-";
  const pairOk = camera.pair_ok ?? "-";
  const posePositive = camera.pose_positive ?? "-";
  const poseScore = camera.pose_score ?? "-";
  const eventActive = camera.event_active ?? "-";
  const latestEventStatus = camera.latest_event_status || "-";
  const latestEventId = camera.latest_event_id || "-";
  const latestStage3Label = camera.latest_stage3_label || "-";
  const latestStage3Prob = camera.latest_stage3_prob ?? "-";
  const queueStatus = camera.queue_status || "-";
  const queueReason = camera.queue_reason || "-";
  const queueSize = camera.queue_size === "" ? "-" : camera.queue_size;
  const queueCapacity = camera.queue_capacity === "" ? "-" : camera.queue_capacity;
  const lastTs = camera.last_ts || "-";

  const imageUrl = `/fight-detection/dashboard/stream/${encodeURIComponent(cameraId)}/`;

  return `
    <div class="camera-card">
      <div class="camera-card__header">
        <strong>${escapeHtml(cameraId)}</strong>
        ${buildStatusBadge(camera)}
      </div>

      <div class="camera-card__stream">
        <img
          src="${imageUrl}"
          alt="${escapeHtml(cameraId)}"
          onerror="this.style.display='none'; this.nextElementSibling.style.display='block';"
        />
        <div class="stream-fallback" style="display:none;">
          <div>${escapeHtml(source)}</div>
          <small>Görüntü açılamadı</small>
        </div>
      </div>

      <div class="camera-card__meta">
        ${buildMetaBox("Stage", stage)}
        ${buildMetaBox("Detail", detail)}
        ${buildMetaBox("Kişi", persons)}
        ${buildMetaBox("Pair", pairOk)}
        ${buildMetaBox("Pose+", posePositive)}
        ${buildMetaBox("Pose Score", formatNumber(poseScore))}
        ${buildMetaBox("Event Active", eventActive)}
        ${buildMetaBox("Event Status", latestEventStatus)}
        ${buildMetaBox("Event ID", latestEventId)}
        ${buildMetaBox("Stage3 Label", latestStage3Label)}
        ${buildMetaBox("Stage3 Prob", formatNumber(latestStage3Prob))}
        ${buildMetaBox("Queue", queueStatus)}
        ${buildMetaBox("Queue Reason", queueReason)}
        ${buildMetaBox("Queue Size", `${queueSize} / ${queueCapacity}`)}
        ${buildMetaBox("Last TS", lastTs)}
        ${buildMetaBox("Kaynak", source)}
      </div>
    </div>
  `;
}

function buildEmptyState(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

async function fetchStatus() {
  try {
    const response = await fetch("/fight-detection/dashboard/status/");

    if (!response.ok) {
      if (cameraCards) {
        cameraCards.innerHTML = buildEmptyState("Durum bilgisi alınamadı.");
      }
      updateLiveStatusBadge({ running: false });
      return;
    }

    const data = await response.json();

    updateLiveStatusBadge(data.pipeline);

    const cameras = Array.isArray(data.cameras) ? data.cameras : [];

    if (!cameraCards) return;

    if (!cameras.length) {
      cameraCards.innerHTML = buildEmptyState("Henüz aktif kamera yok.");
      return;
    }

    cameraCards.innerHTML = cameras.map(buildCard).join("");
  } catch (error) {
    console.error("status fetch error:", error);

    if (cameraCards) {
      cameraCards.innerHTML = buildEmptyState("Durum bilgisi alınamadı.");
    }

    updateLiveStatusBadge({ running: false });
  }
}

async function startMonitoring() {
  const sources = collectSources();

  if (!sources.length) {
    alert("En az bir kamera için Kamera ID ve Kaynak girmen gerekiyor.");
    return;
  }

  const duplicated = new Set();
  const seen = new Set();

  for (const item of sources) {
    if (seen.has(item.camera_id)) {
      duplicated.add(item.camera_id);
    }
    seen.add(item.camera_id);
  }

  if (duplicated.size > 0) {
    alert("Aynı Kamera ID birden fazla kez kullanılamaz.");
    return;
  }

  try {
    if (startBtn) startBtn.disabled = true;

    const response = await fetch("/fight-detection/dashboard/start/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify({ sources }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error("start response error:", errorText);
      alert("Başlatma sırasında hata oluştu.");
      return;
    }

    const data = await response.json();
    console.log("start response:", data);

    await fetchStatus();
  } catch (error) {
    console.error("start error:", error);
    alert("Başlatma isteği gönderilemedi.");
  } finally {
    if (startBtn) startBtn.disabled = false;
  }
}

async function stopMonitoring() {
  try {
    if (stopBtn) stopBtn.disabled = true;

    const response = await fetch("/fight-detection/dashboard/stop/", {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken,
      },
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error("stop response error:", errorText);
      alert("Durdurma sırasında hata oluştu.");
      return;
    }

    const data = await response.json();
    console.log("stop response:", data);

    await fetchStatus();
  } catch (error) {
    console.error("stop error:", error);
    alert("Durdurma isteği gönderilemedi.");
  } finally {
    if (stopBtn) stopBtn.disabled = false;
  }
}

if (addRowBtn) {
  addRowBtn.addEventListener("click", () => createRow());
}

if (clearRowsBtn) {
  clearRowsBtn.addEventListener("click", () => {
    if (!cameraRows) return;
    cameraRows.innerHTML = "";
    createRow();
  });
}

if (startBtn) {
  startBtn.addEventListener("click", startMonitoring);
}

if (stopBtn) {
  stopBtn.addEventListener("click", stopMonitoring);
}

if (rowTemplate && cameraRows && !document.querySelector(".camera-row")) {
  createRow();
}

fetchStatus();
setInterval(fetchStatus, 2000);