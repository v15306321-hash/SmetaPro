// Графики кабинета на Chart.js. Данные — в data-атрибутах <canvas>. Палитра: слоты 1–3 (проверены на CVD).
(function () {
  if (!window.Chart) return;
  const css = getComputedStyle(document.documentElement);
  const C = {
    s1: css.getPropertyValue("--series-1").trim(),
    s2: css.getPropertyValue("--series-2").trim(),
    s3: css.getPropertyValue("--series-3").trim(),
    grid: "#eef0f3", axis: "#868c98", ink: "#15171c",
  };
  const fmt = (v) => Math.round(v).toLocaleString("ru-RU").replace(/,/g, " ");
  const short = (v) => {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1).replace(".", ",") + " млрд";
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(".", ",") + " млн";
    if (a >= 1e3) return Math.round(v / 1e3) + " тыс";
    return String(v);
  };
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.color = C.axis;
  const tooltip = {
    backgroundColor: "#fff", titleColor: C.ink, bodyColor: "#4f5562", borderColor: "#e5e7eb", borderWidth: 1,
    padding: 10, cornerRadius: 10, boxPadding: 4, usePointStyle: true,
  };
  const read = (el, k) => JSON.parse(el.dataset[k] || "null");

  document.querySelectorAll("canvas[data-spark]").forEach((el) => {
    const data = read(el, "spark");
    const color = C[el.dataset.color || "s1"];
    new Chart(el, {
      type: "line",
      data: { labels: data.map((_, i) => i + 1), datasets: [{ data, borderColor: color, borderWidth: 2, tension: .35,
        pointRadius: 0, fill: true, backgroundColor: color + "1f" }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false, beginAtZero: true } } },
    });
  });

  document.querySelectorAll("canvas[data-bars]").forEach((el) => {
    const { labels, values } = read(el, "bars");
    const horizontal = el.dataset.horizontal === "1";
    const unit = el.dataset.unit || "сум";
    new Chart(el, {
      type: "bar",
      data: { labels, datasets: [{ data: values, backgroundColor: C[el.dataset.color || "s1"], borderRadius: 4,
        borderSkipped: "start", maxBarThickness: 36, categoryPercentage: .7 }] },
      options: {
        indexAxis: horizontal ? "y" : "x", responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { ...tooltip, displayColors: false,
          callbacks: { label: (c) => fmt(c.parsed[horizontal ? "x" : "y"]) + " " + unit } } },
        scales: {
          [horizontal ? "x" : "y"]: { beginAtZero: true, grid: { color: C.grid }, border: { display: false },
            ticks: { callback: (v) => unit === "%" ? v + "%" : short(v), maxTicksLimit: horizontal ? 4 : 6, maxRotation: 0 } },
          [horizontal ? "y" : "x"]: { grid: { display: false }, border: { color: "#e5e7eb" },
            ticks: { autoSkip: false, maxRotation: 0, callback: function (v) {
              const l = String(this.getLabelForValue(v)); const n = horizontal ? 18 : 11;
              return l.length > n ? l.slice(0, n - 1) + "…" : l; } } },
        },
      },
    });
  });

  document.querySelectorAll("canvas[data-lines]").forEach((el) => {
    const { labels, series } = read(el, "lines");
    new Chart(el, {
      type: "line",
      data: { labels, datasets: series.map((s, i) => ({
        label: s.label, data: s.data, borderColor: C["s" + (i + 1)], backgroundColor: C["s" + (i + 1)],
        borderWidth: 2, tension: .3, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderColor: "#fff",
        pointHoverBorderWidth: 2 })) },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false }, tooltip: { ...tooltip,
          callbacks: { label: (c) => " " + c.dataset.label + ": " + fmt(c.parsed.y) + " сум" } } },
        scales: {
          y: { beginAtZero: true, grid: { color: C.grid }, border: { display: false },
            ticks: { callback: short, maxTicksLimit: 6 } },
          x: { grid: { display: false }, border: { color: "#e5e7eb" } },
        },
      },
    });
  });
})();
