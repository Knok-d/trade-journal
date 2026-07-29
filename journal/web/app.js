/* Дневник: рендер без фреймворков. Все данные вставляются через textContent —
   заметки пишет пользователь, и они не должны исполняться как разметка. */

"use strict";

const state = { days: 0, from: null, to: null, pendingOnly: false,
                series: null, tags: { rule: [], reason: [] } };

/* ---------- утилиты ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtUsd(x) {
  if (x === null || x === undefined) return "—";
  const sign = x > 0 ? "+" : "";
  return sign + x.toFixed(2);
}

function fmtPrice(x) {
  if (x === null || x === undefined) return "—";
  if (x >= 100) return x.toFixed(2);
  if (x >= 1) return x.toFixed(4);
  return x.toPrecision(4);
}

function fmtPct(x) {
  return Math.round(x * 100) + "%";
}

function fmtPct2(x) {
  if (x === null || x === undefined) return "—";
  return (x > 0 ? "+" : "") + (x * 100).toFixed(1) + "%";
}

function fmtDate(ms) {
  return new Date(ms).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function pnlClass(x) {
  if (x === null || x === undefined || x === 0) return "";
  return x > 0 ? "pos" : "neg";
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ": " + r.status);
  return r.json();
}

async function postJSON(url, payload) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

/* ---------- KPI ---------- */

function kpi(label, value, opts = {}) {
  const card = el("div", "kpi" + (opts.headline ? " headline" : ""));
  card.append(el("div", "label", label));
  const v = el("div", "value" + (opts.cls ? " " + opts.cls : ""), value);
  card.append(v);
  if (opts.hint) card.append(el("div", "hint" + (opts.warn ? " warn" : ""), opts.hint));
  return card;
}

/* В режиме приложения синк живёт в этом же процессе и знает, чем кончился
   последний круг, — это точнее отметки в базе. Порог «три часа» там был бы
   вредным: при круге раз в минуту он молчит два с половиной часа поломки,
   а после сна мака, наоборот, кричит на исправный синк. */
function freshnessBanner(data) {
  const sync = data.sync;
  if (sync) {
    if (sync.last_error) {
      return el("div", "stale-banner", "Синхронизация не проходит: " + sync.last_error);
    }
    if (sync.last_ok === null) {
      return el("div", "stale-banner ok", "Первый круг синхронизации…");
    }
    const minutes = Math.round((Date.now() - sync.last_ok) / 60000);
    return el("div", "stale-banner ok",
      minutes < 2 ? "Данные свежие" : "Обновлено " + minutes + " мин назад");
  }

  const f = data.freshness;
  if (!f || !f.stale) return null;
  return el("div", "stale-banner", f.synced_at === null
    ? "Данные ни разу не обновлялись с биржи"
    : "Данные не обновлялись " + Math.round(f.age_hours) + " ч — синхронизация не работает");
}

function renderKpis(data) {
  const s = data.summary;
  const box = document.getElementById("kpis");
  box.replaceChildren();

  // Старый баннер снимается всегда, а не только когда данные стали свежими:
  // сводка перерисовывается на каждое сохранение, и баннеры копились стопкой.
  const old = document.querySelector(".stale-banner");
  if (old) old.remove();

  const banner = freshnessBanner(data);
  if (banner) box.parentNode.insertBefore(banner, box);

  if (!s.n) {
    box.append(el("div", "empty", "Закрытых сделок за период нет."));
    return;
  }

  const cov = data.coverage;
  // Заголовочная метрика продукта (решение C) — разобранность, не P&L.
  box.append(kpi(
    "Разобрано сделок",
    cov.annotated + " / " + cov.trades,
    {
      headline: true,
      hint: "с намерением до входа: " + cov.with_intent,
    }
  ));

  // Матожидание на сделку убрано отдельной плашкой: это тот же итог, делённый
  // на число сделок. Ценным в ней был только интервал — накрывает он ноль или
  // нет, — и он переехал сюда, к самому итогу.
  const [elo, ehi] = s.expectancy_ci;
  const crossesZero = elo < 0 && ehi > 0;
  box.append(kpi("Итог за период", fmtUsd(s.total) + " USDT", {
    cls: pnlClass(s.total),
    hint: s.n + " сделок · " + fmtUsd(s.expectancy) + " на сделку" +
      (crossesZero ? ", интервал накрывает ноль" : ""),
    warn: crossesZero,
  }));

  const [wlo, whi] = s.win_rate_ci;
  box.append(kpi("Прибыльных", fmtPct(s.win_rate), {
    hint: "95% интервал " + fmtPct(wlo) + "–" + fmtPct(whi),
  }));

  if (s.payoff !== null) {
    const needed = (1 - s.win_rate) / s.win_rate;
    const under = s.payoff < needed;
    box.append(kpi("Прибыль / убыток", s.payoff.toFixed(2), {
      hint: "для безубытка нужно " + needed.toFixed(2),
      warn: under,
      cls: under ? "neg" : "",
    }));
  }

  box.append(kpi("Издержки", fmtUsd(-s.costs) + " USDT", {
    cls: "neg",
    hint: "комиссии " + s.fees.toFixed(2) + " · фандинг " + fmtUsd(s.funding),
  }));
}

/* ---------- топ прибыльных сделок ---------- */

function renderTopTrades(top) {
  const box = document.getElementById("top-trades");
  const sub = document.getElementById("top-sub");
  box.replaceChildren();

  if (!top.trades.length) {
    sub.textContent = "";
    box.append(el("div", "empty", "Прибыльных сделок за период нет."));
    return;
  }

  sub.textContent = top.trades.length + " из " + top.winners_total +
    " прибыльных · вместе " + fmtUsd(top.top_sum) + " USDT" +
    (top.share_of_wins !== null
      ? " = " + fmtPct(top.share_of_wins) + " всей прибыли"
      : "");

  const peak = top.trades[0].net_pnl || 1;
  for (const t of top.trades) {
    const row = el("div", "hist-row");
    const label = el("div", "hist-range");
    label.append(el("span", "top-symbol", t.symbol));
    label.append(el("span", "top-dir", " " + t.direction));
    row.append(label);
    const track = el("div", "hist-track");
    const bar = el("div", "hist-bar pos");
    bar.style.width = Math.max(2, t.net_pnl / peak * 100).toFixed(1) + "%";
    track.append(bar);
    row.append(track);
    row.append(el("div", "hist-count pos", fmtUsd(t.net_pnl)));
    box.append(row);
  }
}

/* ---------- графики ----------
   Рисуются руками в SVG: ноль зависимостей — принцип проекта, и он же
   действует на фронте.

   Размер берётся из настоящей ширины контейнера, а не растягивается через
   preserveAspectRatio="none": при растяжении круги превращаются в овалы,
   а скругления у столбиков — в наклонные срезы. Отсюда перерисовка по
   ResizeObserver. */

const SVG_NS = "http://www.w3.org/2000/svg";
const CHART_H = 168;
// Сверху с запасом: над самым высоким столбиком должна помещаться пилюля со
// значением, иначе она вылезает за карточку и обрезается заголовком.
const PAD = { top: 38, right: 12, bottom: 22, left: 46 };

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function chartFrame(box) {
  const width = Math.max(240, Math.round(box.clientWidth));
  const root = svg("svg", {
    width, height: CHART_H,
    viewBox: `0 0 ${width} ${CHART_H}`,
    role: "img",
  });
  const tip = el("div", "tip");
  tip.hidden = true;
  box.replaceChildren(root, tip);
  return { root, tip, width, height: CHART_H };
}

function fmtShort(v) {
  const abs = Math.abs(v);
  if (abs >= 1000) return (v / 1000).toFixed(abs >= 10000 ? 0 : 1) + "k";
  return String(Math.round(v));
}

function fmtDay(ms) {
  return new Date(ms).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

/* Горизонтальные линии сетки с подписями. Четыре штуки: больше превращает
   график в миллиметровку, меньше — заставляет угадывать масштаб. */
function grid(root, width, lo, hi, y) {
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const value = lo + (hi - lo) * i / steps;
    root.append(svg("line", {
      x1: PAD.left, x2: width - PAD.right, y1: y(value), y2: y(value),
      class: "grid",
    }));
    const label = svg("text", {
      x: PAD.left - 8, y: y(value) + 4, class: "tick", "text-anchor": "end",
    });
    label.textContent = fmtShort(value);
    root.append(label);
  }
}

function xLabels(root, width, height, from, to, x) {
  for (const [at, anchor] of [[from, "start"], [to, "end"]]) {
    const label = svg("text", {
      x: x(at), y: height - 6, class: "tick", "text-anchor": anchor,
    });
    label.textContent = fmtDay(at);
    root.append(label);
  }
}

/* Монотонная кубическая интерполяция: сглаживает, но не выносит кривую за
   пределы самих значений. Обычный сплайн нарисовал бы прибыль, которой не
   было, — на графике денег это недопустимо. */
function smoothPath(pts) {
  if (pts.length < 2) return "";
  const n = pts.length;
  const dx = [], dy = [], slope = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1].x - pts[i].x);
    dy.push(pts[i + 1].y - pts[i].y);
    slope.push(dy[i] / (dx[i] || 1));
  }
  const m = [slope[0]];
  for (let i = 1; i < n - 1; i++) {
    m.push(slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2);
  }
  m.push(slope[n - 2]);
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / slope[i], b = m[i + 1] / slope[i];
    const h = Math.hypot(a, b);
    if (h > 3) { m[i] = 3 * a * slope[i] / h; m[i + 1] = 3 * b * slope[i] / h; }
  }
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 0; i < n - 1; i++) {
    const t = dx[i] / 3;
    d += ` C ${pts[i].x + t} ${pts[i].y + m[i] * t},` +
         ` ${pts[i + 1].x - t} ${pts[i + 1].y - m[i + 1] * t},` +
         ` ${pts[i + 1].x} ${pts[i + 1].y}`;
  }
  return d;
}

function showTip(tip, text, left, top, sign) {
  tip.textContent = text;
  tip.className = "tip " + sign;
  tip.hidden = false;
  // Пилюля не должна вылезать за пределы карточки, поэтому край подпирается.
  const half = tip.offsetWidth / 2;
  const max = tip.parentElement.clientWidth - half;
  tip.style.left = Math.min(Math.max(left, half), max) + "px";
  tip.style.top = Math.max(0, top) + "px";
}

function renderEquity(points) {
  const box = document.getElementById("equity");
  const sub = document.getElementById("equity-sub");
  if (!points || points.length < 2) {
    sub.textContent = "";
    box.replaceChildren(el("div", "empty", "Сделок за период недостаточно для кривой."));
    return;
  }

  const { root, tip, width, height } = chartFrame(box);
  const xs = points.map((p) => p.at);
  const ys = points.map((p) => p.cum);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const x = (t) => PAD.left + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) *
    (width - PAD.left - PAD.right);
  const y = (v) => height - PAD.bottom - (v - lo) / span * (height - PAD.top - PAD.bottom);

  grid(root, width, lo, hi, y);
  xLabels(root, width, height, x0, x1, x);

  const last = ys[ys.length - 1];
  const tone = last >= 0 ? "pos" : "neg";
  const gradientId = "equity-fill";
  const defs = svg("defs", {});
  const gradient = svg("linearGradient", {
    id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1,
  });
  gradient.append(svg("stop", { offset: "0%", class: "grad-top " + tone }));
  gradient.append(svg("stop", { offset: "100%", class: "grad-bottom " + tone }));
  defs.append(gradient);
  root.append(defs);

  const pts = points.map((p) => ({ x: x(p.at), y: y(p.cum) }));
  const line = smoothPath(pts);
  root.append(svg("path", {
    d: `${line} L ${x(x1)} ${y(lo)} L ${x(x0)} ${y(lo)} Z`,
    fill: `url(#${gradientId})`, stroke: "none",
  }));
  root.append(svg("path", { d: line, class: "line " + tone }));

  // Ноль отдельной линией: подъём с −900 до −800 без него читается прибылью.
  if (lo < 0 && hi > 0) {
    root.append(svg("line", {
      x1: PAD.left, x2: width - PAD.right, y1: y(0), y2: y(0), class: "zero",
    }));
  }

  const guide = svg("line", { class: "guide", y1: PAD.top, y2: height - PAD.bottom });
  const dot = svg("circle", { r: 5, class: "dot " + tone });
  guide.setAttribute("opacity", 0);
  dot.setAttribute("opacity", 0);
  root.append(guide, dot);

  root.addEventListener("pointermove", (event) => {
    const rect = root.getBoundingClientRect();
    const at = x0 + (event.clientX - rect.left - PAD.left) /
      (width - PAD.left - PAD.right) * (x1 - x0);
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      if (Math.abs(points[i].at - at) < Math.abs(points[best].at - at)) best = i;
    }
    const p = points[best];
    guide.setAttribute("x1", x(p.at));
    guide.setAttribute("x2", x(p.at));
    guide.setAttribute("opacity", 1);
    dot.setAttribute("cx", x(p.at));
    dot.setAttribute("cy", y(p.cum));
    dot.setAttribute("opacity", 1);
    showTip(tip, fmtUsd(p.pnl) + "  ·  итог " + fmtUsd(p.cum),
            x(p.at), y(p.cum) - 34, pnlClass(p.pnl));
  });
  root.addEventListener("pointerleave", () => {
    guide.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
    tip.hidden = true;
  });

  sub.textContent = "итог " + fmtUsd(last) + " · просадка " +
    fmtUsd(-maxDrawdown(ys)) + " USDT";
}

/* Максимальная просадка — глубина самого болезненного отката от пика.
   Итог её не показывает: −900 в конце может быть и ровным сползанием,
   и качелями на полторы тысячи. */
function maxDrawdown(values) {
  let peak = values[0], worst = 0;
  for (const v of values) {
    if (v > peak) peak = v;
    worst = Math.max(worst, peak - v);
  }
  return worst;
}

function renderDaily(points) {
  const box = document.getElementById("daily");
  const sub = document.getElementById("daily-sub");
  if (!points || !points.length) {
    sub.textContent = "";
    box.replaceChildren(el("div", "empty", "Сделок за период нет."));
    return;
  }

  // Сутки режутся по местному времени, а не по UTC: сделка в два часа ночи
  // принадлежит этой ночи, а не вчерашнему дню сервера.
  const byDay = new Map();
  for (const p of points) {
    const d = new Date(p.at);
    const key = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const cell = byDay.get(key) || { sum: 0, n: 0 };
    cell.sum += p.pnl;
    cell.n += 1;
    byDay.set(key, cell);
  }
  const days = [...byDay.entries()].sort((a, b) => a[0] - b[0]);

  const { root, tip, width, height } = chartFrame(box);
  const values = days.map(([, c]) => c.sum);
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const span = hi - lo || 1;
  const y = (v) => height - PAD.bottom - (v - lo) / span * (height - PAD.top - PAD.bottom);

  grid(root, width, lo, hi, y);
  xLabels(root, width, height, days[0][0], days[days.length - 1][0],
          (t) => PAD.left + (days.findIndex(([d]) => d === t) + 0.5) *
            ((width - PAD.left - PAD.right) / days.length));

  const step = (width - PAD.left - PAD.right) / days.length;
  const barWidth = Math.max(4, Math.min(22, step * 0.68));
  const zero = y(0);
  const bars = [];

  // Ось нуля: столбики расходятся вверх и вниз от неё, и без линии непонятно,
  // где кончается прибыльный день и начинается убыточный.
  if (lo < 0 && hi > 0) {
    root.append(svg("line", {
      x1: PAD.left, x2: width - PAD.right, y1: zero, y2: zero, class: "zero",
    }));
  }

  days.forEach(([day, cell], i) => {
    const value = cell.sum;
    const top = value >= 0 ? y(value) : zero;
    const size = Math.max(2, Math.abs(y(value) - zero));
    const bar = svg("rect", {
      x: PAD.left + i * step + (step - barWidth) / 2,
      y: top, width: barWidth, height: size,
      // Скругление во всю ширину — как в референсе; на низких столбиках
      // радиус подрезается по высоте, иначе прямоугольник схлопывается в каплю.
      rx: Math.min(barWidth / 2, size / 2),
      class: "bar " + (value >= 0 ? "pos" : "neg"),
    });
    root.append(bar);
    bars.push({ bar, day, value, n: cell.n, cx: PAD.left + i * step + step / 2, top });
  });

  root.addEventListener("pointermove", (event) => {
    const rect = root.getBoundingClientRect();
    const i = Math.floor((event.clientX - rect.left - PAD.left) / step);
    const hit = bars[Math.max(0, Math.min(bars.length - 1, i))];
    // Остальные дни глушатся, чтобы выбранный читался сразу.
    for (const b of bars) b.bar.classList.toggle("dim", b !== hit);
    showTip(tip, fmtDay(hit.day) + ": " + fmtUsd(hit.value) + " · " +
            hit.n + (hit.n === 1 ? " сделка" : " сдел."),
            hit.cx, hit.top - 34, pnlClass(hit.value));
  });
  root.addEventListener("pointerleave", () => {
    for (const b of bars) b.bar.classList.remove("dim");
    tip.hidden = true;
  });

  const wins = values.filter((v) => v > 0).length;
  sub.textContent = days.length + " дней · в плюс " + wins;
}

/* Ширина известна только из DOM, поэтому при изменении размера окна графики
   перерисовываются целиком. Данные лежат в state — второй раз их не просят. */
function redrawCharts() {
  if (!state.series) return;
  renderEquity(state.series);
  renderDaily(state.series);
}

const chartsObserver = new ResizeObserver(() => redrawCharts());
chartsObserver.observe(document.querySelector(".charts"));

/* ---------- открытые позиции ---------- */

function renderOpen(open) {
  const panel = document.getElementById("open-panel");
  const body = document.getElementById("open-body");
  const sub = document.getElementById("open-sub");

  if (!open || !open.positions.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  body.replaceChildren();

  for (const p of open.positions) {
    const tr = el("tr");
    tr.append(el("td", null, p.symbol));
    tr.append(el("td", "dir", p.direction));
    tr.append(el("td", "num", String(p.qty)));
    tr.append(el("td", "num", fmtPrice(p.avg_entry)));
    tr.append(el("td", "num", fmtPrice(p.mark_price)));
    tr.append(el("td", "num lev", p.leverage ? p.leverage + "×" : "—"));
    tr.append(el("td", "num " + pnlClass(p.unrealised), fmtUsd(p.unrealised)));
    tr.append(el("td", "num lev", p.liq_price ? fmtPrice(p.liq_price) : "—"));
    body.append(tr);
  }

  // Возраст снимка обязателен: нереализованный P&L десятиминутной давности,
  // показанный как текущий, — худшее, что может показать дневник.
  const age = open.taken_at === null
    ? "неизвестно, когда снято"
    : "снято " + Math.round((Date.now() - open.taken_at) / 60000) + " мин назад";
  sub.textContent = open.positions.length + " шт. · " + age;
}

/* ---------- правила и основания ----------
   Один и тот же компонент на два вида отметок: правило нарушают, основание
   применяют, а список, архивация и цифры устроены одинаково. Панель находит
   себя по data-kind, поэтому вторая появилась без единой новой функции. */

const KIND_WORDS = {
  rule: {
    none: "нарушений нет",
    empty: "Правил пока нет. Напиши те, которым сам себя обязал.",
    hint: "Нарушения отмечаются галочками при разборе сделки.",
    with: "с нарушением", without: "без нарушений",
  },
  reason: {
    none: "не применялось",
    empty: "Заготовок пока нет. Добавь те, что пишешь чаще всего.",
    hint: "Основания отмечаются галочками при разборе сделки.",
    with: "с этим основанием", without: "без основания",
  },
};

async function saveTag(kind, payload) {
  await postJSON("/api/tag", { kind, ...payload });
  await loadTags();
  loadSummary().catch(showError);
}

function tagRow(kind, tag, measured) {
  const li = el("li", "rule" + (tag.active ? "" : " archived"));

  // Текст — сразу поле ввода: правится на месте, без режима редактирования.
  const input = el("input", "rule-text");
  input.type = "text";
  input.value = tag.body;
  input.maxLength = 200;
  input.disabled = !tag.active;
  input.setAttribute("aria-label", "Текст");
  input.addEventListener("change", () => {
    if (!input.value.trim() || input.value === tag.body) {
      input.value = tag.body;
      return;
    }
    saveTag(kind, { id: tag.id, body: input.value }).catch(showError);
  });

  const stat = el("span", "rule-stat");
  if (measured) {
    stat.textContent = measured.n
      ? measured.n + " сдел. · " + fmtUsd(measured.total)
      : KIND_WORDS[kind].none;
    if (measured.n && measured.total < 0) stat.classList.add("neg");
  }

  const archive = el("button", "rule-archive", tag.active ? "×" : "↩");
  archive.type = "button";
  archive.title = tag.active ? "В архив" : "Вернуть из архива";
  archive.setAttribute("aria-label", archive.title);
  archive.addEventListener("click", () => {
    saveTag(kind, { id: tag.id, active: !tag.active }).catch(showError);
  });

  li.append(input, stat, archive);
  return li;
}

function renderTagPanel(kind, measured) {
  const panel = document.querySelector(`.panel[data-kind="${kind}"]`);
  const words = KIND_WORDS[kind];
  const byId = {};
  for (const t of measured.tags) byId[t.id] = t;

  const list = panel.querySelector("[data-list]");
  list.replaceChildren();
  // Архивная строка показывается, только пока за ней числятся отметки:
  // иначе список зарастает передуманным.
  for (const tag of state.tags[kind]) {
    if (tag.active || byId[tag.id]) list.append(tagRow(kind, tag, byId[tag.id]));
  }

  panel.querySelector("[data-sub]").textContent =
    measured.of_total ? "разобрано " + measured.reviewed + " из " + measured.of_total : "";

  const box = panel.querySelector("[data-summary]");
  box.replaceChildren();
  // Пока список пуст, сравнивать нечего: все сделки формально «без отметок».
  if (state.tags[kind].length && (measured.violated.n || measured.clean.n)) {
    box.append(el("div", "rules-cmp",
      words.with + ": " + measured.violated.n + " сдел., средний " +
      fmtUsd(measured.violated.avg) + "  ·  " + words.without + ": " +
      measured.clean.n + " сдел., средний " + fmtUsd(measured.clean.avg)));
    if (!measured.enough) {
      // Разница между группами на маленькой выборке — шум, и об этом надо
      // сказать прямо: иначе «правило, которое стоит мне денег» найдётся всегда.
      box.append(el("div", "rules-warn",
        "в группах меньше " + measured.min_n +
        " сделок — разница между ними пока ничего не доказывает"));
    }
  }

  panel.querySelector("[data-hint]").textContent =
    state.tags[kind].length ? words.hint : words.empty;
}

function renderRules(data) {
  renderTagPanel("rule", data.rules);
  renderTagPanel("reason", data.reasons);
}

/* Галочки отметок в раскрытой строке сделки. */
function markGroup(kind, trade, title) {
  const active = state.tags[kind].filter((t) => t.active);
  if (!active.length) return null;

  const field = kind === "rule" ? "violations" : "reasons";
  const box = el("div", "violations");
  box.append(el("div", "violations-title", title));
  for (const tag of active) {
    const label = el("label", "violation");
    const check = el("input");
    check.type = "checkbox";
    check.checked = trade[field].includes(tag.id);
    check.addEventListener("change", async () => {
      check.disabled = true;
      try {
        await postJSON("/api/mark", {
          kind, trade_id: trade.trade_id, id: tag.id, on: check.checked,
        });
        trade[field] = check.checked
          ? trade[field].concat(tag.id)
          : trade[field].filter((id) => id !== tag.id);
        loadSummary().catch(showError);
      } catch (err) {
        check.checked = !check.checked;   // не притворяемся, что сохранилось
      } finally {
        check.disabled = false;
      }
    });
    label.append(check, el("span", null, tag.body));
    box.append(label);
  }
  return box;
}

/* ---------- таблица сделок ---------- */

function detailRow(t) {
  const tr = el("tr", "detail");
  const td = el("td");
  td.colSpan = 9;   // столбцов в шапке таблицы
  const grid = el("div", "detail-grid");

  grid.append(el("div", "detail-meta",
    "объём " + t.qty + " · комиссия " + t.fees.toFixed(2) +
    " · фандинг " + fmtUsd(t.funding) +
    (t.fees_source === "exchange" ? " · комиссия от биржи (MNT)" : "") +
    (t.liquidated ? " · ЛИКВИДАЦИЯ" : "")
  ));

  if (t.has_intent) {
    const box = el("div", "intent-box");
    box.append(el("div", "tag", "намерение до входа" +
      (t.planned_stop ? " · стоп " + t.planned_stop : "") +
      (t.match_note ? " · " + t.match_note : "")));
    box.append(el("div", null, t.thesis || ""));
    grid.append(box);
  }

  const reasons = markGroup("reason", t, "На чём заходил:");
  if (reasons) grid.append(reasons);

  const broken = markGroup("rule", t, "Какие правила нарушены:");
  if (broken) grid.append(broken);

  const editor = el("div", "note-editor");
  const label = el("label", null, "Почему заходил, что увидел, что пошло не так:");
  label.htmlFor = "note-" + t.trade_id;
  const area = el("textarea");
  area.id = "note-" + t.trade_id;
  area.value = t.note || "";
  const actions = el("div", "note-actions");
  const save = el("button", null, "Сохранить разбор");
  save.type = "button";
  const status = el("span", "note-status", "");
  actions.append(save, status);
  editor.append(label, area, actions);
  grid.append(editor);

  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "…";
    try {
      await postJSON("/api/note", { trade_id: t.trade_id, body: area.value });
      t.note = area.value.trim() || null;
      status.textContent = t.note ? "сохранено" : "очищено";
      // KPI зависят и от summary, поэтому дешевле перечитать сводку целиком,
      // чем точечно править карточку разобранности.
      loadSummary().catch(showError);
      refreshRowBadge(t);
    } catch (err) {
      status.textContent = "ошибка сохранения: " + err.message;
    } finally {
      save.disabled = false;
    }
  });

  td.append(grid);
  tr.append(td);
  return tr;
}

function badgeFor(t) {
  if (t.note) return el("span", "badge noted", "разобрана");
  if (t.has_intent) return el("span", "badge intent", "план до входа");
  return el("span", "badge", "—");
}

function refreshRowBadge(t) {
  const btn = document.getElementById("badge-" + t.trade_id);
  if (btn) btn.replaceChildren(badgeFor(t));
}

function renderTrades(trades) {
  const body = document.getElementById("trades-body");
  body.replaceChildren();
  document.getElementById("trades-count").textContent =
    trades.length ? "закрытых: " + trades.length : "";

  if (!trades.length) {
    const tr = el("tr");
    const td = el("td", "empty",
      state.pendingOnly ? "Все сделки за период разобраны." : "Сделок нет.");
    td.colSpan = 9;   // столбцов в шапке таблицы
    tr.append(td);
    body.append(tr);
    return;
  }

  for (const t of trades) {
    const tr = el("tr", "trade");

    tr.append(el("td", null, fmtDate(t.closed_at)));
    tr.append(el("td", null, t.symbol));
    tr.append(el("td", "dir", t.direction));
    tr.append(el("td", "num", fmtPrice(t.avg_entry)));
    tr.append(el("td", "num", fmtPrice(t.avg_exit)));
    tr.append(el("td", "num lev", t.leverage ? t.leverage + "×" : "—"));
    tr.append(el("td", "num " + pnlClass(t.net_pnl), fmtUsd(t.net_pnl)));
    // Прочерк, а не 0%: сделки без пары в closed-pnl биржи бывают, и знаменателя
    // для процента у них просто нет.
    tr.append(el("td", "num " + pnlClass(t.roi), fmtPct2(t.roi)));

    // Раскрытие — настоящая кнопка, а не кликабельный tr: tr с tabindex
    // не появляется в дереве доступности как интерактивный элемент.
    const badgeCell = el("td");
    const btn = el("button", "badge-btn");
    btn.type = "button";
    btn.id = "badge-" + t.trade_id;
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", "Разбор сделки " + t.symbol);
    btn.append(badgeFor(t));
    badgeCell.append(btn);
    tr.append(badgeCell);

    let open = null;
    const toggle = () => {
      if (open) {
        open.remove();
        open = null;
        btn.setAttribute("aria-expanded", "false");
      } else {
        open = detailRow(t);
        tr.after(open);
        btn.setAttribute("aria-expanded", "true");
        open.querySelector("textarea").focus();
      }
    };
    btn.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
    tr.addEventListener("click", toggle);

    body.append(tr);
  }
}

/* ---------- загрузка ---------- */

function periodQuery() {
  if (state.from !== null || state.to !== null) {
    return "from=" + (state.from ?? "") + "&to=" + (state.to ?? "");
  }
  return "days=" + state.days;
}

async function loadSummary() {
  const data = await getJSON("/api/summary?" + periodQuery());
  renderOpen(data.open);
  state.series = data.series;
  redrawCharts();
  renderKpis(data);
  renderTopTrades(data.top_trades);
  renderRules(data);
}

async function loadTags() {
  const [rules, reasons] = await Promise.all([
    getJSON("/api/tags?kind=rule"),
    getJSON("/api/tags?kind=reason"),
  ]);
  state.tags = { rule: rules.tags, reason: reasons.tags };
}

async function loadTrades() {
  const data = await getJSON(
    "/api/trades?" + periodQuery() + "&pending=" + (state.pendingOnly ? 1 : 0));
  renderTrades(data.trades);
}

function loadAll() {
  // Правила грузятся первыми: и сводка, и раскрытая строка сделки рисуют
  // по ним — чекбоксы нарушений и подписи.
  loadTags()
    .then(() => Promise.all([loadSummary(), loadTrades()]))
    .catch(showError);
}

function showError(err) {
  const box = document.getElementById("kpis");
  box.replaceChildren(el("div", "empty", "Ошибка загрузки: " + err.message));
}

/* ---------- период ---------- */

const dateFrom = document.getElementById("date-from");
const dateTo = document.getElementById("date-to");
const datesClear = document.getElementById("dates-clear");

function applyDates() {
  // Конец дня, а не начало: иначе «по 29 июля» отрезало бы весь этот день.
  state.from = dateFrom.value ? new Date(dateFrom.value + "T00:00:00").getTime() : null;
  state.to = dateTo.value ? new Date(dateTo.value + "T23:59:59.999").getTime() : null;

  const manual = state.from !== null || state.to !== null;
  datesClear.hidden = !manual;
  document.querySelectorAll(".periods button").forEach((b) => {
    if (manual) b.removeAttribute("aria-current");
  });
  if (!manual) {
    const active = document.querySelector(`.periods button[data-days="${state.days}"]`);
    if (active) active.setAttribute("aria-current", "true");
  }
  loadAll();
}

document.querySelectorAll(".periods button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.days = Number(btn.dataset.days);
    // Пресет и отрезок — одно и то же поле выбора, поэтому пресет чистит даты:
    // иначе кнопка нажималась бы без всякого видимого эффекта.
    state.from = state.to = null;
    dateFrom.value = dateTo.value = "";
    datesClear.hidden = true;
    document.querySelectorAll(".periods button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    loadAll();
  });
});

dateFrom.addEventListener("change", applyDates);
dateTo.addEventListener("change", applyDates);
datesClear.addEventListener("click", () => {
  dateFrom.value = dateTo.value = "";
  applyDates();
});

document.getElementById("only-pending").addEventListener("change", (e) => {
  state.pendingOnly = e.target.checked;
  loadTrades().catch(showError);
});

document.querySelectorAll(".panel[data-kind] [data-add]").forEach((form) => {
  const kind = form.closest(".panel").dataset.kind;
  const input = form.querySelector("input");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    saveTag(kind, { body: input.value })
      .then(() => { input.value = ""; })
      .catch(showError);
  });
});

loadAll();
