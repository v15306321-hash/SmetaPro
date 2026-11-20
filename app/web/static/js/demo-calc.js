// Демонстрационный калькулятор на главной. Условные цены — только для иллюстрации.
(function () {
  const form = document.getElementById("calc-form");
  if (!form) return;
  const RATES = { ldsp: 1900000, "3d": 2900000, oak: 5400000, eco: 2300000, patina: 3400000,
                  enamel: 3600000, photo: 2600000, veneer: 3900000, combo: 3100000 };
  const SIDES = { line: ["a"], l: ["a", "b"], u: ["a", "b", "c"], par: ["a", "b"] };
  const PATHS = {
    line: "M20 37h110",
    l: "M20 15h110M20 15v50",
    u: "M20 64V15h110v49",
    par: "M20 18h110M20 56h110",
  };
  const T = Object.assign({ body: "Корпус и фасады", hw: "Фурнитура и комплектующие", work: "Сборка и монтаж",
    empty: "Укажите размеры", mp: "м.п.", cur: "сум" }, window.CALC_TEXT || {});
  let shape = "u";
  const fmt = (n) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ".") + " " + T.cur;
  const num = (v) => parseFloat(String(v || "").replace(",", ".")) || 0;

  function setShape(s) {
    shape = s;
    form.querySelectorAll(".shape").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.shape === s)));
    document.getElementById("diagram").innerHTML = `<path d="${PATHS[s]}"/>`;
    ["a", "b", "c"].forEach((k) => {
      const inp = form.elements[k];
      inp.disabled = !SIDES[s].includes(k);
    });
    calc();
  }

  function calc() {
    const meters = SIDES[shape].reduce((sum, k) => sum + num(form.elements[k].value), 0);
    const facade = form.elements.facade.value;
    const label = form.querySelector(`input[name=facade][value="${facade}"]`).nextElementSibling.textContent;
    const body = meters * RATES[facade];
    const hw = body * 0.18;
    const work = body * 0.12;
    const lines = [
      [`${T.body} (${label}), ${meters.toFixed(1).replace(".", ",")} ${T.mp}`, body],
      [T.hw, hw],
      [T.work, work],
    ];
    document.getElementById("calc-lines").innerHTML = meters
      ? lines.map(([n, v]) => `<li><span>${n}</span><span>${fmt(v)}</span></li>`).join("")
      : `<li><span>${T.empty}</span><span></span></li>`;
    document.getElementById("calc-total").textContent = meters ? fmt(body + hw + work) : "—";
  }

  form.querySelectorAll(".shape").forEach((b) => b.addEventListener("click", () => setShape(b.dataset.shape)));
  form.addEventListener("input", calc);
  document.getElementById("calc-btn").addEventListener("click", calc);
  form.elements.b.value = "2,6";
  form.elements.c.value = "2,1";
  setShape("u");
})();
