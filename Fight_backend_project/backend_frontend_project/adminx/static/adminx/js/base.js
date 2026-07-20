const adminBaseRoot = document.getElementById("admin-base");

const adminBaseConfig = {
  fightStatusUrl: adminBaseRoot?.dataset.fightStatusUrl || "",
  speedStatusUrl: adminBaseRoot?.dataset.speedStatusUrl || "",
};

function setModuleStatus(prefix, payload) {
  const card = document.getElementById(`admin-${prefix}-status-card`);
  const icon = document.getElementById(`admin-${prefix}-status-icon`);
  const text = document.getElementById(`admin-${prefix}-status-text`);
  const runningEl = document.getElementById(`admin-${prefix}-running`);
  const cameraCountEl = document.getElementById(`admin-${prefix}-camera-count`);

  if (!card || !text || !runningEl || !cameraCountEl) return;

  const running = Boolean(payload.running);

  if (running) {
    card.classList.add("is-running");
    card.classList.remove("is-stopped", "is-error");

    if (icon) icon.textContent = "●";

    text.textContent = prefix === "fight"
      ? "Kavga tespit pipeline aktif olarak çalışıyor."
      : "Hız tespit pipeline aktif olarak çalışıyor.";

    runningEl.textContent = "Aktif";
  } else {
    card.classList.add("is-stopped");
    card.classList.remove("is-running", "is-error");

    if (icon) icon.textContent = "●";

    text.textContent = prefix === "fight"
      ? "Kavga tespit pipeline şu anda durdurulmuş."
      : "Hız tespit pipeline şu anda durdurulmuş.";

    runningEl.textContent = "Durduruldu";
  }

  cameraCountEl.textContent = payload.camera_count ?? payload.source_count ?? 0;
}

function setModuleError(prefix) {
  const card = document.getElementById(`admin-${prefix}-status-card`);
  const text = document.getElementById(`admin-${prefix}-status-text`);
  const runningEl = document.getElementById(`admin-${prefix}-running`);
  const cameraCountEl = document.getElementById(`admin-${prefix}-camera-count`);

  if (card) {
    card.classList.add("is-error");
    card.classList.remove("is-running", "is-stopped");
  }

  if (text) {
    text.textContent = "Sistem durumu okunamadı.";
  }

  if (runningEl) {
    runningEl.textContent = "Bilinmiyor";
  }

  if (cameraCountEl) {
    cameraCountEl.textContent = "-";
  }
}

async function fetchJson(url) {
  if (!url) {
    throw new Error("Status URL bulunamadı.");
  }

  const response = await fetch(url, {
    method: "GET",
    headers: {
      "X-Requested-With": "XMLHttpRequest",
    },
    cache: "no-store",
    credentials: "same-origin",
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const data = await response.json();

  if (!data.ok) {
    throw new Error("Geçersiz sistem yanıtı.");
  }

  return data;
}

async function updateAdminSystemStatus() {
  try {
    const fightData = await fetchJson(adminBaseConfig.fightStatusUrl);
    setModuleStatus("fight", fightData);
  } catch (error) {
    setModuleError("fight");
    console.error("Kavga sistem durumu hatası:", error);
  }

  try {
    const speedData = await fetchJson(adminBaseConfig.speedStatusUrl);
    setModuleStatus("speed", speedData);
  } catch (error) {
    setModuleError("speed");
    console.error("Hız sistem durumu hatası:", error);
  }
}

document.addEventListener("DOMContentLoaded", function () {
  updateAdminSystemStatus();
  setInterval(updateAdminSystemStatus, 5000);
});