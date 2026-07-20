const adminDashboardRoot = document.getElementById("admin-dashboard");

const adminDashboardConfig = {
  fightStartUrl: adminDashboardRoot?.dataset.fightStartUrl || "",
  fightStopUrl: adminDashboardRoot?.dataset.fightStopUrl || "",
  speedStartUrl: adminDashboardRoot?.dataset.speedStartUrl || "",
  speedStopUrl: adminDashboardRoot?.dataset.speedStopUrl || "",
};

function numberFromDataset(name) {
  if (!adminDashboardRoot) return 0;

  const value = Number(adminDashboardRoot.dataset[name] || "0");

  if (Number.isNaN(value)) {
    return 0;
  }

  return value;
}

const dashboardData = {
  cameraCount: numberFromDataset("cameraCount"),
  activeCameraCount: numberFromDataset("activeCameraCount"),
  passiveCameraCount: numberFromDataset("passiveCameraCount"),

  fightCameraCount: numberFromDataset("fightCameraCount"),
  speedCameraCount: numberFromDataset("speedCameraCount"),

  fightIncidentCount: numberFromDataset("fightIncidentCount"),
  speedRecordCount: numberFromDataset("speedRecordCount"),

  approvedUserCount: numberFromDataset("approvedUserCount"),
  pendingUserCount: numberFromDataset("pendingUserCount"),
  rejectedUserCount: numberFromDataset("rejectedUserCount"),
};

function getCookie(name) {
  const value = `; ${document.cookie}`;
  const parts = value.split(`; ${name}=`);

  if (parts.length === 2) {
    return parts.pop().split(";").shift();
  }

  return "";
}

function showControlMessage(id, message, type) {
  const el = document.getElementById(id);

  if (!el) return;

  el.textContent = message;
  el.className = "module-control-message is-visible";

  if (type === "success") {
    el.classList.add("is-success");
  }

  if (type === "error") {
    el.classList.add("is-error");
  }
}

async function postPipelineCommand(url) {
  if (!url) {
    throw new Error("İstek URL bilgisi bulunamadı.");
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "X-CSRFToken": getCookie("fight_csrftoken") || getCookie("csrftoken"),
      "X-Requested-With": "XMLHttpRequest",
    },
    cache: "no-store",
    credentials: "same-origin",
  });

  const text = await response.text();

  let data = null;

  try {
    data = text ? JSON.parse(text) : null;
  } catch (err) {
    data = null;
  }

  if (!response.ok) {
    throw new Error(data?.error || data?.message || text || "İstek başarısız");
  }

  return data;
}

async function sendModuleCommand(options) {
  const buttons = options.buttonIds
    .map((id) => document.getElementById(id))
    .filter(Boolean);

  try {
    buttons.forEach((btn) => {
      btn.disabled = true;
    });

    const data = await postPipelineCommand(options.url);

    showControlMessage(
      options.messageId,
      data?.message || options.successMessage,
      "success",
    );

    if (typeof updateAdminSystemStatus === "function") {
      updateAdminSystemStatus();
    }
  } catch (error) {
    showControlMessage(
      options.messageId,
      `İşlem gerçekleştirilemedi: ${error.message}`,
      "error",
    );

    console.error("Modül kontrol hatası:", error);
  } finally {
    buttons.forEach((btn) => {
      btn.disabled = false;
    });
  }
}

const chartTheme = {
  grid: "rgba(148, 163, 184, 0.22)",
  text: "#64748b",
  primary: "#1677ff",
  cyan: "#0ea5e9",
  success: "#16a34a",
  danger: "#dc2626",
  warning: "#d97706",
  speed: "#f97316",
  slate: "#64748b",
};

function createDoughnutChart(canvasId, labels, values, colors) {
  const el = document.getElementById(canvasId);

  if (!el || typeof Chart === "undefined") return;

  new Chart(el, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [
        {
          data: values,
          backgroundColor: colors,
          borderColor: "#ffffff",
          borderWidth: 3,
          hoverOffset: 8,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "64%",
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: chartTheme.text,
            usePointStyle: true,
            pointStyle: "circle",
            padding: 18,
            font: {
              size: 12,
              weight: "700",
            },
          },
        },
      },
    },
  });
}

function createBarChart(canvasId, labels, values, colors) {
  const el = document.getElementById(canvasId);

  if (!el || typeof Chart === "undefined") return;

  new Chart(el, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [
        {
          data: values,
          backgroundColor: colors,
          borderRadius: 14,
          borderSkipped: false,
          maxBarThickness: 54,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: false,
        },
      },
      scales: {
        x: {
          grid: {
            display: false,
          },
          ticks: {
            color: chartTheme.text,
            font: {
              size: 12,
              weight: "700",
            },
          },
        },
        y: {
          beginAtZero: true,
          grid: {
            color: chartTheme.grid,
          },
          ticks: {
            color: chartTheme.text,
            precision: 0,
          },
        },
      },
    },
  });
}

document.addEventListener("DOMContentLoaded", function () {
  const fightStart = document.getElementById("admin-fight-start-btn");
  const fightStop = document.getElementById("admin-fight-stop-btn");
  const speedStart = document.getElementById("admin-speed-start-btn");
  const speedStop = document.getElementById("admin-speed-stop-btn");

  if (fightStart) {
    fightStart.addEventListener("click", function () {
      sendModuleCommand({
        url: adminDashboardConfig.fightStartUrl,
        successMessage: "Kavga tespit sistemi başlatıldı.",
        messageId: "admin-fight-control-message",
        buttonIds: ["admin-fight-start-btn", "admin-fight-stop-btn"],
      });
    });
  }

  if (fightStop) {
    fightStop.addEventListener("click", function () {
      sendModuleCommand({
        url: adminDashboardConfig.fightStopUrl,
        successMessage: "Kavga tespit sistemi durduruldu.",
        messageId: "admin-fight-control-message",
        buttonIds: ["admin-fight-start-btn", "admin-fight-stop-btn"],
      });
    });
  }

  if (speedStart) {
    speedStart.addEventListener("click", function () {
      sendModuleCommand({
        url: adminDashboardConfig.speedStartUrl,
        successMessage: "Hız tespit sistemi başlatıldı.",
        messageId: "admin-speed-control-message",
        buttonIds: ["admin-speed-start-btn", "admin-speed-stop-btn"],
      });
    });
  }

  if (speedStop) {
    speedStop.addEventListener("click", function () {
      sendModuleCommand({
        url: adminDashboardConfig.speedStopUrl,
        successMessage: "Hız tespit sistemi durduruldu.",
        messageId: "admin-speed-control-message",
        buttonIds: ["admin-speed-start-btn", "admin-speed-stop-btn"],
      });
    });
  }

  createDoughnutChart(
    "cameraStatusChart",
    ["Aktif Kamera", "Pasif Kamera"],
    [dashboardData.activeCameraCount, dashboardData.passiveCameraCount],
    [chartTheme.success, chartTheme.danger],
  );

  createDoughnutChart(
    "moduleChart",
    ["Kavga Kamerası", "Hız Kamerası"],
    [dashboardData.fightCameraCount, dashboardData.speedCameraCount],
    [chartTheme.primary, chartTheme.speed],
  );

  createBarChart(
    "recordChart",
    ["Kavga Kaydı", "Hız Kaydı"],
    [dashboardData.fightIncidentCount, dashboardData.speedRecordCount],
    [chartTheme.primary, chartTheme.speed],
  );

  createBarChart(
    "userApprovalChart",
    ["Onaylı", "Bekleyen", "Reddedilen"],
    [
      dashboardData.approvedUserCount,
      dashboardData.pendingUserCount,
      dashboardData.rejectedUserCount,
    ],
    [chartTheme.success, chartTheme.warning, chartTheme.danger],
  );
});