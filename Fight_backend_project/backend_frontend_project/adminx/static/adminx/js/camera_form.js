const cameraFormPage = document.getElementById("camera-form-page");

const cameraFormConfig = {
  sourceModeName: cameraFormPage?.dataset.sourceModeName || "",
  speedModuleCheckboxId: cameraFormPage?.dataset.speedModuleCheckboxId || "",
  hasSpeedForm: cameraFormPage?.dataset.hasSpeedForm === "1",
  roiHiddenInputId: cameraFormPage?.dataset.roiHiddenInputId || "",
};

function updateCameraSourcePanels() {
  if (!cameraFormConfig.sourceModeName) return;

  const selected = document.querySelector(
    `input[name="${CSS.escape(cameraFormConfig.sourceModeName)}"]:checked`,
  );
  const manualPanel = document.getElementById("manualSourcePanel");
  const uploadPanel = document.getElementById("uploadSourcePanel");

  if (!selected || !manualPanel || !uploadPanel) {
    return;
  }

  if (selected.value === "upload") {
    manualPanel.classList.remove("is-visible");
    uploadPanel.classList.add("is-visible");
  } else {
    uploadPanel.classList.remove("is-visible");
    manualPanel.classList.add("is-visible");
  }
}

function updateSpeedModuleConfigVisibility() {
  const speedModuleCheckbox = document.getElementById(
    cameraFormConfig.speedModuleCheckboxId,
  );
  const speedConfigWrapper = document.getElementById("speedModuleConfigWrapper");

  if (!speedModuleCheckbox || !speedConfigWrapper) {
    return;
  }

  if (speedModuleCheckbox.checked) {
    speedConfigWrapper.classList.add("is-visible");
  } else {
    speedConfigWrapper.classList.remove("is-visible");
  }
}

function csrfToken() {
  const names = ["fight_csrftoken", "csrftoken"];

  for (const name of names) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);

    if (parts.length === 2) {
      return parts.pop().split(";").shift();
    }
  }

  return "";
}

function initCameraSourcePanels() {
  if (!cameraFormConfig.sourceModeName) return;

  const radios = document.querySelectorAll(
    `input[name="${CSS.escape(cameraFormConfig.sourceModeName)}"]`,
  );

  radios.forEach(function (radio) {
    radio.addEventListener("change", updateCameraSourcePanels);
  });

  const speedModuleCheckbox = document.getElementById(
    cameraFormConfig.speedModuleCheckboxId,
  );

  if (speedModuleCheckbox) {
    speedModuleCheckbox.addEventListener("change", updateSpeedModuleConfigVisibility);
  }

  updateCameraSourcePanels();
  updateSpeedModuleConfigVisibility();
}

function initSpeedCalibration() {
  if (!cameraFormConfig.hasSpeedForm) return;

  const canvas = document.getElementById("speed-cal-canvas");
  const emptyState = document.getElementById("speed-cal-empty-state");
  const loadBtn = document.getElementById("speed-cal-load-btn");
  const roiDoneBtn = document.getElementById("speed-cal-roi-done-btn");
  const undoBtn = document.getElementById("speed-cal-undo-btn");
  const resetBtn = document.getElementById("speed-cal-reset-btn");
  const saveBtn = document.getElementById("speed-cal-save-btn");
  const stepText = document.getElementById("speed-cal-step-text");
  const helpText = document.getElementById("speed-cal-help-text");
  const distanceInput = document.getElementById("speed-cal-distance-input");
  const frameUrlInput = document.getElementById("speed-cal-frame-url");
  const saveUrlInput = document.getElementById("speed-cal-save-url");
  const roiHiddenInput = cameraFormConfig.roiHiddenInputId
    ? document.getElementById(cameraFormConfig.roiHiddenInputId)
    : null;

  if (!canvas || !frameUrlInput || !saveUrlInput) return;

  const ctx = canvas.getContext("2d");
  const image = new Image();

  let imageLoaded = false;
  let step = "roi";
  let roi = [];
  let lineA = [];
  let lineB = [];

  const stepOrder = ["roi", "line_a", "line_b", "distance"];

  function parseExistingRoi() {
    if (!roiHiddenInput) return;

    try {
      const raw = roiHiddenInput.value || "[]";
      const parsed = JSON.parse(raw);

      if (!Array.isArray(parsed)) return;

      roi = parsed
        .filter(function (p) {
          return Array.isArray(p) && p.length === 2;
        })
        .map(function (p) {
          return [Number(p[0]), Number(p[1])];
        })
        .filter(function (p) {
          return Number.isFinite(p[0]) && Number.isFinite(p[1]);
        });
    } catch (err) {
      roi = [];
    }
  }

  function syncStepPills() {
    const currentIndex = stepOrder.indexOf(step);

    document.querySelectorAll("[data-speed-step-pill]").forEach(function (el) {
      const pillStep = el.getAttribute("data-speed-step-pill");
      const pillIndex = stepOrder.indexOf(pillStep);

      el.classList.toggle("is-active", pillStep === step);
      el.classList.toggle("is-done", pillIndex >= 0 && pillIndex < currentIndex);
    });
  }

  function setStep(nextStep) {
    step = nextStep;

    const labels = {
      roi: [
        "Adım: Yol bölgesi / ROI seçimi",
        "Yol alanı için en az 3 nokta seç, sonra ROI Bitti butonuna bas.",
      ],
      line_a: [
        "Adım: Başlangıç çizgisi",
        "Aracın önce geçeceği çizgi için 2 nokta seç.",
      ],
      line_b: [
        "Adım: Bitiş çizgisi",
        "Aracın sonra geçeceği çizgi için 2 nokta seç.",
      ],
      distance: [
        "Adım: Gerçek mesafe",
        "İki çizgi arasındaki gerçek mesafeyi metre cinsinden gir ve kaydet.",
      ],
    };

    const pair = labels[step] || labels.roi;

    if (stepText) stepText.textContent = pair[0];
    if (helpText) helpText.textContent = pair[1];

    syncStepPills();
  }

  function syncRoiHidden() {
    if (!roiHiddenInput) return;

    roiHiddenInput.value = JSON.stringify(
      roi.map(function (p) {
        return [Math.round(p[0]), Math.round(p[1])];
      }),
    );
  }

  function drawPoint(point, color, label) {
    ctx.beginPath();
    ctx.arc(point[0], point[1], 6, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();

    if (label) {
      ctx.font = "bold 12px Arial";
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "rgba(15, 23, 42, 0.75)";
      ctx.lineWidth = 4;
      ctx.strokeText(label, point[0] + 9, point[1] - 9);
      ctx.fillText(label, point[0] + 9, point[1] - 9);
    }
  }

  function drawLine(points, color, label) {
    points.forEach(function (p, index) {
      drawPoint(p, color, `${label}.${String(index + 1)}`);
    });

    if (points.length >= 2) {
      ctx.beginPath();
      ctx.moveTo(points[0][0], points[0][1]);
      ctx.lineTo(points[1][0], points[1][1]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 4;
      ctx.stroke();

      ctx.font = "bold 16px Arial";
      ctx.fillStyle = color;
      ctx.strokeStyle = "rgba(15, 23, 42, 0.85)";
      ctx.lineWidth = 5;
      ctx.strokeText(label, points[0][0] + 10, points[0][1] - 12);
      ctx.fillText(label, points[0][0] + 10, points[0][1] - 12);
    }
  }

  function drawRoi() {
    if (roi.length <= 0) return;

    ctx.beginPath();

    roi.forEach(function (p, idx) {
      if (idx === 0) {
        ctx.moveTo(p[0], p[1]);
      } else {
        ctx.lineTo(p[0], p[1]);
      }
    });

    if (roi.length >= 3) {
      ctx.closePath();
      ctx.fillStyle = "rgba(34, 197, 94, 0.18)";
      ctx.fill();
    }

    ctx.lineWidth = 3;
    ctx.strokeStyle = "#22c55e";
    ctx.stroke();

    roi.forEach(function (p, idx) {
      drawPoint(p, "#22c55e", `R${String(idx + 1)}`);
    });
  }

  function draw() {
    syncRoiHidden();

    if (!imageLoaded) return;

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);

    drawRoi();
    drawLine(lineA, "#facc15", "START");
    drawLine(lineB, "#e879f9", "END");
  }

  function canvasPoint(event) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;

    return [
      Math.round((event.clientX - rect.left) * scaleX),
      Math.round((event.clientY - rect.top) * scaleY),
    ];
  }

  function loadFrame() {
    if (!frameUrlInput.value) {
      alert("Kalibrasyon görüntüsü için önce kamerayı kaydetmelisin.");
      return;
    }

    image.onload = function () {
      canvas.width = image.naturalWidth || image.width;
      canvas.height = image.naturalHeight || image.height;
      canvas.style.display = "block";

      if (emptyState) {
        emptyState.style.display = "none";
      }

      imageLoaded = true;
      draw();
    };

    image.onerror = function () {
      alert("Kalibrasyon görüntüsü alınamadı. Kamera kaynağını kontrol et.");
    };

    image.src = `${frameUrlInput.value}?t=${Date.now()}`;
  }

  function resetAll() {
    roi = [];
    lineA = [];
    lineB = [];

    if (distanceInput) {
      distanceInput.value = "";
    }

    setStep("roi");
    draw();
  }

  function undo() {
    if (step === "roi") {
      roi.pop();
    } else if (step === "line_a") {
      lineA.pop();
    } else if (step === "line_b") {
      lineB.pop();
    } else if (step === "distance") {
      if (lineB.length > 0) {
        lineB.pop();
        setStep("line_b");
      } else if (lineA.length > 0) {
        lineA.pop();
        setStep("line_a");
      }
    }

    draw();
  }

  async function saveCalibration() {
    const distance = Number(distanceInput ? distanceInput.value : "");

    if (roi.length < 3) {
      alert("Yol bölgesi / ROI için en az 3 nokta seçmelisin.");
      setStep("roi");
      return;
    }

    if (lineA.length !== 2) {
      alert("Başlangıç çizgisi için 2 nokta seçmelisin.");
      setStep("line_a");
      return;
    }

    if (lineB.length !== 2) {
      alert("Bitiş çizgisi için 2 nokta seçmelisin.");
      setStep("line_b");
      return;
    }

    if (!Number.isFinite(distance) || distance <= 0) {
      alert("Gerçek mesafeyi metre olarak gir.");
      setStep("distance");
      return;
    }

    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = "Kaydediliyor...";
    }

    try {
      const response = await fetch(saveUrlInput.value, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({
          roi_polygon: roi,
          line_a: lineA,
          line_b: lineB,
          distance_m: distance,
          direction: "A_TO_B",
        }),
      });

      const data = await response.json().catch(function () {
        return null;
      });

      if (!response.ok || !data || !data.ok) {
        alert(data?.message || "Kalibrasyon kaydedilemedi.");
        return;
      }

      syncRoiHidden();
      alert("Kalibrasyon kaydedildi. Diğer kamera ayarları için formu da Güncelle diyerek kaydet.");
    } catch (err) {
      console.error("Kalibrasyon kaydetme hatası:", err);
      alert("Kalibrasyon kaydedilemedi. Django terminalini kontrol et.");
    } finally {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = "Kalibrasyonu Kaydet";
      }
    }
  }

  if (loadBtn) {
    loadBtn.addEventListener("click", loadFrame);
  }

  if (roiDoneBtn) {
    roiDoneBtn.addEventListener("click", function () {
      if (roi.length < 3) {
        alert("Yol bölgesi / ROI için en az 3 nokta seçmelisin.");
        return;
      }

      setStep("line_a");
      draw();
    });
  }

  if (undoBtn) {
    undoBtn.addEventListener("click", undo);
  }

  if (resetBtn) {
    resetBtn.addEventListener("click", resetAll);
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", saveCalibration);
  }

  canvas.addEventListener("click", function (event) {
    if (!imageLoaded) return;

    const p = canvasPoint(event);

    if (step === "roi") {
      roi.push(p);
    } else if (step === "line_a") {
      if (lineA.length < 2) {
        lineA.push(p);
      }

      if (lineA.length === 2) {
        setStep("line_b");
      }
    } else if (step === "line_b") {
      if (lineB.length < 2) {
        lineB.push(p);
      }

      if (lineB.length === 2) {
        setStep("distance");
      }
    }

    draw();
  });

  parseExistingRoi();
  syncRoiHidden();
  setStep("roi");
}

document.addEventListener("DOMContentLoaded", function () {
  initCameraSourcePanels();
  initSpeedCalibration();
});