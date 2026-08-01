/* Дневник: рендер без фреймворков. Все данные вставляются через textContent —
   заметки пишет пользователь, и они не должны исполняться как разметка. */

"use strict";

const state = { days: 0, from: null, to: null, asset: "", pendingOnly: false,
                // Отбор и порядок ТОЛЬКО для таблицы сделок: цифры и графики
                // над ней остаются про весь период. Иначе поиск по тикеру
                // молча превращал бы сводку в отчёт по одной монете.
                symbol: "", sort: "date", tag: null,
                series: null, tags: { rule: [], reason: [] } };

/* Как показывать класс актива. Крипта молчит: её девять десятых, и подпись на
   каждой строке была бы шумом, а не сведением. */
const ASSET_LABEL = { stock: "акция", commodity: "товар", forex: "форекс",
                      cfd: "CFD" };

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
  const card = el("div", "kpi");
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

/* ---------- лучшие и худшие сделки ----------
   Обе половины считаются и рисуются одинаково, отличаются только словами и
   цветом. Концентрация в подзаголовке — не украшение: если весь плюс дали три
   сделки, это разговор про три сделки, а не про систему. */

function renderTradeBars(boxId, subId, trades, words) {
  const box = document.getElementById(boxId);
  const sub = document.getElementById(subId);
  box.replaceChildren();

  // «Поля нет в ответе» и «список пуст» — разные вещи, и путать их нельзя:
  // старый сервер с новой страницей молча выдал бы «убыточных сделок нет»
  // при полном их наличии. Отсутствие данных должно называть себя само.
  if (!trades) {
    sub.textContent = "";
    box.append(el("div", "empty",
      "Сервер не прислал эти данные: он запущен со старой версией кода."));
    return;
  }

  if (!trades.length) {
    sub.textContent = "";
    box.append(el("div", "empty", words.empty));
    return;
  }

  sub.textContent = trades.length + " из " + words.total + " " + words.of +
    " · вместе " + fmtUsd(words.sum) + " USDT" +
    (words.share !== null && words.share !== undefined
      ? " = " + fmtPct(words.share) + " " + words.whole
      : "");

  // Знаменатель — самая крупная сделка половины. У убытков обе величины
  // отрицательные, и отношение выходит положительным само собой.
  const peak = trades[0].net_pnl || 1;
  for (const t of trades) {
    const row = el("div", "hist-row");
    const label = el("div", "hist-range");
    label.append(el("span", "top-symbol", t.symbol));
    label.append(el("span", "top-dir", " " + t.direction));
    row.append(label);
    const track = el("div", "hist-track");
    const bar = el("div", "hist-bar " + words.cls);
    bar.style.width = Math.max(2, t.net_pnl / peak * 100).toFixed(1) + "%";
    track.append(bar);
    row.append(track);
    row.append(el("div", "hist-count " + words.cls, fmtUsd(t.net_pnl)));
    box.append(row);
  }
}

function renderTopTrades(top) {
  renderTradeBars("top-trades", "top-sub", top.trades, {
    total: top.winners_total, sum: top.top_sum, share: top.share_of_wins,
    cls: "pos", of: "прибыльных", whole: "всей прибыли",
    empty: "Прибыльных сделок за период нет.",
  });
  renderTradeBars("worst-trades", "worst-sub", top.worst, {
    total: top.losers_total, sum: top.worst_sum, share: top.share_of_losses,
    cls: "neg", of: "убыточных", whole: "всего убытка",
    empty: "Убыточных сделок за период нет.",
  });
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

/* Точка на самой отрисованной кривой по координате x.

   Считать её из данных нельзя: линия сглажена, и точка, поставленная по
   исходному значению, повисла бы рядом с кривой, а не на ней. Путь монотонен
   по x (время только растёт), поэтому годится двоичный поиск по длине. */
function pointOnPath(path, targetX) {
  let lo = 0, hi = path.getTotalLength();
  for (let i = 0; i < 16; i++) {
    const mid = (lo + hi) / 2;
    if (path.getPointAtLength(mid).x < targetX) lo = mid; else hi = mid;
  }
  return path.getPointAtLength((lo + hi) / 2);
}

function startOfDay(ms) {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
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
  const curve = svg("path", { d: line, class: "line " + tone });
  root.append(curve);

  // Ноль отдельной линией: подъём с −900 до −800 без него читается прибылью.
  if (lo < 0 && hi > 0) {
    root.append(svg("line", {
      x1: PAD.left, x2: width - PAD.right, y1: y(0), y2: y(0), class: "zero",
    }));
  }

  const guide = svg("line", { class: "guide", y1: PAD.top, y2: height - PAD.bottom });
  // Точка нейтральная и цветом ничего не утверждает: она отмечает, где стоит
  // курсор. Раньше её красили по итогу всего периода, и над зелёной точкой
  // могла висеть красная пилюля убыточного дня — один цвет означал сразу две
  // разные вещи. Знак теперь говорит только пилюля, и только про свой день.
  const dot = svg("circle", { r: 5, class: "dot" });
  guide.setAttribute("opacity", 0);
  dot.setAttribute("opacity", 0);
  root.append(guide, dot);

  // Что произошло в каждый календарный день — чтобы подсказка могла честно
  // сказать «сделок не было», а не молчать о днях, где кривая просто ровная.
  const perDay = new Map();
  for (const p of points) {
    const key = startOfDay(p.at);
    const cell = perDay.get(key) || { sum: 0, n: 0 };
    cell.sum += p.pnl;
    cell.n += 1;
    perDay.set(key, cell);
  }

  root.addEventListener("pointermove", (event) => {
    const rect = root.getBoundingClientRect();
    const left = PAD.left, right = width - PAD.right;
    // Точка едет за курсором по кривой, а не прыгает к ближайшей сделке.
    // Прыжки и были причиной, по которой до дней без сделок было не добраться:
    // на длинном ровном участке курсор всё равно утаскивало к соседней сделке.
    const px = Math.min(Math.max(event.clientX - rect.left, left), right);
    const at = x0 + (px - left) / (right - left) * (x1 - x0);

    // Накопленный итог на этот момент — по последней сделке не позже него.
    let cum = 0;
    for (const p of points) {
      if (p.at > at) break;
      cum = p.cum;
    }

    const day = perDay.get(startOfDay(at));
    const onCurve = pointOnPath(curve, px);
    guide.setAttribute("x1", px);
    guide.setAttribute("x2", px);
    guide.setAttribute("opacity", 1);
    dot.setAttribute("cx", px);
    dot.setAttribute("cy", onCurve.y);
    dot.setAttribute("opacity", 1);

    const what = day
      ? fmtUsd(day.sum) + " за день · " + day.n + (day.n === 1 ? " сделка" : " сдел.")
      : "сделок не было";
    showTip(tip, fmtDay(at) + " · " + what + " · итог " + fmtUsd(cum),
            px, onCurve.y - 34, day ? pnlClass(day.sum) : "");
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
// Наблюдаем за самими графиками, а не за общей обёрткой: после разделения на
// вкладки кривая эквити живёт на главной, а столбики по дням — в аналитике,
// и общего родителя у них больше нет.
for (const box of document.querySelectorAll(".chart")) chartsObserver.observe(box);

/* ---------- открытые позиции ---------- */

/* Список один, а мест показа два: главная отвечает на «что сейчас», сделки —
   «что сейчас и что было». Поэтому рисуем во все контейнеры разом, а не
   держим две копии разметки, которые однажды разойдутся. */
function renderOpen(open) {
  const panels = document.querySelectorAll("[data-open-panel]");
  const empty = !open || !open.positions.length;

  // Возраст снимка обязателен: нереализованный P&L десятиминутной давности,
  // показанный как текущий, — худшее, что может показать дневник.
  const age = empty || open.taken_at === null
    ? "неизвестно, когда снято"
    : "снято " + Math.round((Date.now() - open.taken_at) / 60000) + " мин назад";

  for (const panel of panels) {
    panel.hidden = empty;
    if (empty) continue;

    const body = panel.querySelector("[data-open-body]");
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
    panel.querySelector("[data-open-sub]").textContent =
      open.positions.length + " шт. · " + age;
  }
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

  // Клик по цифрам отбирает таблицу: «покажи те самые сделки» — первое, что
  // хочется сделать, увидев «1 сдел. · +26.79». Кнопка, а не клик по строке:
  // текст рядом правится на месте, и строка целиком кликаться не должна.
  const pick = el("button", "rule-pick");
  pick.type = "button";
  pick.title = "Показать эти сделки";
  pick.setAttribute("aria-label", "Показать сделки, где отмечено: " + tag.body);
  pick.append(stat);
  pick.addEventListener("click", () => selectTag({ kind, id: tag.id }));

  const archive = el("button", "rule-archive", tag.active ? "×" : "↩");
  archive.type = "button";
  archive.title = tag.active ? "В архив" : "Вернуть из архива";
  archive.setAttribute("aria-label", archive.title);
  archive.addEventListener("click", () => {
    saveTag(kind, { id: tag.id, active: !tag.active }).catch(showError);
  });

  li.dataset.tagId = tag.id;
  if (state.tag !== null && state.tag.id === tag.id) li.classList.add("picked");
  li.append(input, pick, archive);
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
        // Основание само по себе делает сделку разобранной, поэтому бейдж
        // обязан ответить сразу — иначе отметка выглядит не сработавшей.
        trade.reviewed = Boolean(
          trade.note || trade.has_intent || trade.reasons.length);
        refreshRowBadge(trade);
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

/* Содержимое разбора. Раньше оно жило в раскрытой строке таблицы, теперь — в
   боковой панели: строится ровно так же, изменилось только место, куда его
   вставляют. Разбору нужна ширина — в строку не помещались ни чекбоксы двух
   списков, ни заведение своей отметки. */
function detailBody(t) {
  const grid = el("div", "detail-grid");

  grid.append(el("div", "detail-meta",
    // У открытой сделки даты закрытия нет, а знать, сколько она уже висит,
    // нужно именно сейчас — поэтому у неё в шапке стоит дата входа.
    (t.closed_at === null ? "открыта " + fmtDate(t.opened_at) + " · " : "") +
    "объём " + t.qty + (t.source === "mt5" ? " лот" : "") +
    " · комиссия " + t.fees.toFixed(2) +
    " · фандинг " + fmtUsd(t.funding) +
    (t.fees_source === "exchange" ? " · комиссия от биржи (MNT)" : "") +
    (t.liquidated ? " · ЛИКВИДАЦИЯ" : "")
  ));

  // Происхождение цифр — не мелочь: по всем остальным сделкам сверка с биржей
  // прошла посделочно, а по этим сравнивать не с чем. Молчание здесь читалось
  // бы как «проверено», и это была бы неправда.
  if (t.source === "mt5") {
    grid.append(el("div", "detail-meta warn",
      "Сделка с MT5: цифры пришли от брокера и в сверку не входят —" +
      " второго источника по ним нет."));
  }

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
  grid.append(addTagForm("reason", "Своя заготовка: «отскок от уровня»"));

  const broken = markGroup("rule", t, "Какие правила нарушены:");
  if (broken) grid.append(broken);
  grid.append(addTagForm("rule", "Своё правило: «не усредняться в убыток»"));

  const editor = el("div", "note-editor");
  // Вопрос по открытой позиции другой: «что пошло не так» ещё не случилось,
  // а план выхода — единственное, что сейчас имеет смысл записать.
  const label = el("label", null, t.closed_at === null
    ? "Почему в позиции, чего ждёшь, где выйдешь:"
    : "Почему заходил, что увидел, что пошло не так:");
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
      // Тот же признак, что считает сервер, но по свежему факту: только что
      // сохранённый текст написан до закрытия ровно тогда, когда сделка ещё
      // открыта. Перечитывать ради этого всю таблицу было бы дороже.
      t.note_before_close = t.note !== null && t.closed_at === null;
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

  return grid;
}

/* Заведение своей отметки прямо в разборе. Раньше форма стояла в панели правил
   рядом со статистикой и отвечала на вопрос «какие у меня вообще правила».
   Нужна она в другой момент: «я только что понял, какое правило нарушил». */
function addTagForm(kind, placeholder) {
  const form = el("form", "rule-add");
  const input = el("input");
  input.type = "text";
  input.maxLength = 200;
  input.placeholder = placeholder;
  input.setAttribute("aria-label", placeholder);
  const btn = el("button", null, "Добавить");
  btn.type = "submit";
  form.append(input, btn);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    btn.disabled = true;
    saveTag(kind, { body: input.value })
      .then(() => {
        input.value = "";
        // Чекбоксы рисуются из state.tags, поэтому список надо перечитать и
        // пересобрать панель — иначе только что заведённое правило появится
        // лишь после перезагрузки страницы.
        return loadTags().then(refreshDrawer);
      })
      .catch(showError)
      .finally(() => { btn.disabled = false; });
  });
  return form;
}

function badgeFor(t) {
  // «До закрытия» — разбор, написанный, когда исход ещё не был известен.
  // Метка живёт ровно до правки после выхода и гаснет сама: запрещать правку
  // незачем, а выдавать поздний текст за ранний нельзя.
  if (t.note) {
    return el("span", "badge noted" + (t.note_before_close ? " early" : ""),
              t.note_before_close ? "разобрана до закрытия" : "разобрана");
  }
  if (t.has_intent) return el("span", "badge intent", "план до входа");
  // Разобранность решает сервер (journal.reviewed_sql). Досюда доходят
  // сделки без текста и без намерения, то есть отмеченные одним основанием.
  if (t.reviewed) return el("span", "badge noted", "основание");
  return el("span", "badge", "—");
}

function refreshRowBadge(t) {
  const btn = document.getElementById("badge-" + t.trade_id);
  if (btn) btn.replaceChildren(badgeFor(t));
}

function renderTrades(trades) {
  const body = document.getElementById("trades-body");
  body.replaceChildren();
  const openCount = trades.filter((t) => t.closed_at === null).length;
  document.getElementById("trades-count").textContent = trades.length
    ? "закрытых: " + (trades.length - openCount) +
      (openCount ? " · в позиции: " + openCount : "")
    : "";

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
    const live = t.closed_at === null;
    const tr = el("tr", live ? "trade live" : "trade");

    tr.append(el("td", null, live ? "в позиции" : fmtDate(t.closed_at)));

    // Класс актива подписью у символа, а не отдельной колонкой: колонка
    // потребовала бы править colSpan раскрытой строки в двух местах.
    const symbolCell = el("td", null, t.symbol);
    if (ASSET_LABEL[t.asset_class]) {
      symbolCell.append(el("span", "asset-tag", ASSET_LABEL[t.asset_class]));
    }
    tr.append(symbolCell);
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
    // Не aria-expanded: строка больше ничего не раскрывает под собой, она
    // открывает диалог сбоку — и озвучивать это надо именно так.
    btn.setAttribute("aria-haspopup", "dialog");
    btn.setAttribute("aria-label", "Разбор сделки " + t.symbol);
    btn.append(badgeFor(t));
    badgeCell.append(btn);
    tr.append(badgeCell);

    const open = () => showDrawer(t, btn);
    btn.addEventListener("click", (e) => { e.stopPropagation(); open(); });
    tr.addEventListener("click", open);

    body.append(tr);
  }
}

/* ---------- загрузка ---------- */

function periodQuery() {
  const asset = state.asset ? "&asset=" + state.asset : "";
  if (state.from !== null || state.to !== null) {
    return "from=" + (state.from ?? "") + "&to=" + (state.to ?? "") + asset;
  }
  return "days=" + state.days + asset;
}

async function loadSummary() {
  const data = await getJSON("/api/summary?" + periodQuery());
  renderOpen(data.open);
  state.series = data.series;
  redrawCharts();
  renderKpis(data);
  renderCards(data);
  renderPnlCalendar();
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

// Фильтров у таблицы пять, и переключают их подряд, не дожидаясь ответа.
// Без номера побеждал бы не последний ЗАПРОС, а последний ОТВЕТ: таблица
// показывала бы отбор, который уже сняли, и выглядела бы при этом исправной.
let tradesTicket = 0;

async function loadTrades() {
  const ticket = ++tradesTicket;
  const extra = [
    "pending=" + (state.pendingOnly ? 1 : 0),
    "sort=" + state.sort,
    state.symbol ? "symbol=" + encodeURIComponent(state.symbol) : "",
    state.tag ? state.tag.kind + "=" + encodeURIComponent(state.tag.id) : "",
  ].filter(Boolean).join("&");
  const data = await getJSON("/api/trades?" + periodQuery() + "&" + extra);
  if (ticket === tradesTicket) renderTrades(data.trades);
}

function loadAll() {
  // Правила грузятся первыми: и сводка, и панель разбора рисуют по ним —
  // чекбоксы нарушений и подписи.
  loadTags()
    .then(() => Promise.all([loadSummary(), loadTrades()]))
    .then(() => {
      const box = document.getElementById("load-error");
      if (box) box.remove();
    })
    .catch(showError);
}

/* Ошибка загрузки писалась в блок KPI. После разделения на вкладки KPI уехали
   в аналитику, и сбой на главной перестал быть виден вообще: страница просто
   оставалась пустой. Плашка кладётся в тот раздел, который сейчас открыт. */
function showError(err) {
  const panel = document.querySelector("[data-panel]:not([hidden])")
    || document.querySelector("[data-panel]");
  if (!panel) return;
  let box = document.getElementById("load-error");
  if (!box) {
    box = el("div", "stale-banner");
    box.id = "load-error";
  }
  box.textContent = "Ошибка загрузки: " + err.message;
  panel.prepend(box);
}

/* ---------- период: свой календарь ----------

   Своими руками, а не <input type="date">, ровно по одной причине: выпадающий
   календарь у нативного поля — элемент браузера, и CSS до него не дотягивается
   вообще. На тёмном дашборде он выглядел чужой заплаткой. Рисуем сами теми же
   переменными, что и всё остальное; заодно отрезок выбирается двумя кликами по
   одной сетке, а не двумя полями по очереди. */

const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const calendar = document.getElementById("calendar");
const datesOpen = document.getElementById("dates-open");
const datesClear = document.getElementById("dates-clear");
let shownMonth = new Date();

function endOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(),
                  23, 59, 59, 999).getTime();
}

function pickDay(date) {
  const start = date.getTime();
  if (state.from === null || state.to !== null) {
    state.from = start;          // новый отрезок: первый клик задаёт начало
    state.to = null;
  } else if (start < state.from) {
    state.from = start;          // кликнули раньше начала — двигаем начало
  } else {
    // Конец дня, а не начало: иначе «по 29 июля» отрезало бы весь этот день.
    state.to = endOfDay(date);
  }
  renderCalendar();
  applyDates();
  if (state.to !== null) toggleCalendar(false);   // отрезок готов — закрываемся
}

function renderCalendar() {
  calendar.replaceChildren();

  const head = el("div", "cal-head");
  const back = el("button", "cal-nav", "‹");
  back.type = "button";
  back.setAttribute("aria-label", "Предыдущий месяц");
  const forward = el("button", "cal-nav", "›");
  forward.type = "button";
  forward.setAttribute("aria-label", "Следующий месяц");
  for (const [btn, step] of [[back, -1], [forward, 1]]) {
    btn.addEventListener("click", () => {
      shownMonth = new Date(shownMonth.getFullYear(), shownMonth.getMonth() + step, 1);
      renderCalendar();
    });
  }
  head.append(back, el("div", "cal-title",
    MONTHS[shownMonth.getMonth()] + " " + shownMonth.getFullYear()), forward);
  calendar.append(head);

  const grid = el("div", "cal-grid");
  for (const day of WEEKDAYS) grid.append(el("div", "cal-weekday", day));

  const year = shownMonth.getFullYear();
  const month = shownMonth.getMonth();
  // Неделя начинается с понедельника, а getDay() даёт 0 для воскресенья.
  const lead = (new Date(year, month, 1).getDay() + 6) % 7;
  const length = new Date(year, month + 1, 0).getDate();
  const today = startOfDay(Date.now());
  const from = state.from === null ? null : startOfDay(state.from);
  const to = state.to === null ? null : startOfDay(state.to);

  for (let i = 0; i < lead; i++) grid.append(el("div", "cal-day blank"));
  for (let number = 1; number <= length; number++) {
    const date = new Date(year, month, number);
    const at = date.getTime();
    const btn = el("button", "cal-day", String(number));
    btn.type = "button";
    if (at === from || at === to) btn.classList.add("edge");
    else if (from !== null && to !== null && at > from && at < to) {
      btn.classList.add("inside");
    }
    if (at === today) btn.classList.add("today");
    // Будущее выбирать нечем: сделок там нет по построению.
    btn.disabled = at > today;
    btn.addEventListener("click", () => pickDay(date));
    grid.append(btn);
  }
  calendar.append(grid);
  calendar.append(el("div", "cal-hint", state.from !== null && state.to === null
    ? "Теперь выбери конец отрезка"
    : "Выбери начало и конец отрезка"));
}

function toggleCalendar(open) {
  calendar.hidden = !open;
  datesOpen.setAttribute("aria-expanded", String(open));
  if (open) {
    // Открываемся на месяце начала отрезка, а не на текущем: поправить уже
    // выбранное — самое частое, ради чего календарь открывают второй раз.
    shownMonth = new Date(state.from === null ? Date.now() : state.from);
    shownMonth = new Date(shownMonth.getFullYear(), shownMonth.getMonth(), 1);
    renderCalendar();
  }
}

function applyDates() {
  const manual = state.from !== null || state.to !== null;
  datesClear.hidden = !manual;
  datesOpen.textContent = manual
    ? fmtDay(state.from) + (state.to === null ? " — …" : " — " + fmtDay(state.to))
    : "Свой отрезок";
  datesOpen.classList.toggle("chosen", manual);
  document.querySelectorAll(".periods button").forEach((b) => {
    if (manual) b.removeAttribute("aria-current");
  });
  if (!manual) {
    const active = document.querySelector(`.periods button[data-days="${state.days}"]`);
    if (active) active.setAttribute("aria-current", "true");
  }
  // Пока выбрано только начало, показывать нечего: отрезок ещё не отрезок.
  if (!manual || state.to !== null) loadAll();
}

datesOpen.addEventListener("click", () => toggleCalendar(calendar.hidden));

// Клик внутри календаря наружу не идёт. Без этого он закрывался на первом же
// выборе дня: обработчик перерисовывает сетку, нажатая кнопка отсоединяется от
// DOM, и проверка ниже видит её уже не внутри календаря — то есть считает
// кликом мимо. Ровно та же ловушка ждала стрелки переключения месяца.
calendar.addEventListener("click", (e) => e.stopPropagation());

// Клик мимо закрывает: обычное поведение всплывающего, и без него календарь
// приходится закрывать той же кнопкой, что неочевидно.
document.addEventListener("click", (e) => {
  if (!calendar.hidden && !calendar.contains(e.target) && e.target !== datesOpen) {
    toggleCalendar(false);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !calendar.hidden) toggleCalendar(false);
});

document.querySelectorAll(".periods button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.days = Number(btn.dataset.days);
    // Пресет и отрезок — одно и то же поле выбора, поэтому пресет чистит даты:
    // иначе кнопка нажималась бы без всякого видимого эффекта.
    state.from = state.to = null;
    datesClear.hidden = true;
    datesOpen.textContent = "Свой отрезок";
    datesOpen.classList.remove("chosen");
    document.querySelectorAll(".periods button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    loadAll();
  });
});

document.querySelectorAll(".assets button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.asset = btn.dataset.asset;
    document.querySelectorAll(".assets button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    // Перезагружается всё: класс актива меняет и сводку, и графики, и таблицу.
    // Перерисовать одну таблицу значило бы показать кривую крипты над списком
    // сделок по золоту.
    loadAll();
  });
});

datesClear.addEventListener("click", () => {
  state.from = state.to = null;
  toggleCalendar(false);
  applyDates();
});

/* ---------- отбор и порядок в таблице сделок ---------- */

document.querySelectorAll(".sorts button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.sort = btn.dataset.sort;
    document.querySelectorAll(".sorts button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    // Только таблица: порядок строк не меняет ни одной цифры над ней.
    loadTrades().catch(showError);
  });
});

const search = document.getElementById("symbol-search");
let searchTimer = null;
search.addEventListener("input", () => {
  // Пауза перед запросом: иначе «btcusdt» — это восемь походов на сервер,
  // и ответы возвращаются вперемешку.
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.symbol = search.value.trim();
    loadTrades().catch(showError);
  }, 250);
});

/* Отметка, по которой отобрана таблица. Показывается плашкой со снятием:
   молчаливый отбор — способ час смотреть на неполный список и не понять. */
function renderTagFilter() {
  const box = document.getElementById("tag-filter");
  box.replaceChildren();
  box.hidden = state.tag === null;
  if (state.tag === null) return;

  const tag = state.tags[state.tag.kind].find((t) => t.id === state.tag.id);
  const words = state.tag.kind === "rule" ? "нарушено правило" : "основание";
  box.append(el("span", "tag-chip-label",
    words + ": " + (tag ? tag.body : state.tag.id)));
  const drop = el("button", "tag-chip-drop", "снять");
  drop.type = "button";
  drop.addEventListener("click", () => selectTag(null));
  box.append(drop);
}

function selectTag(pick) {
  // Повторный клик по той же отметке снимает отбор — тем же движением, каким
  // он ставится.
  const same = state.tag && pick && state.tag.kind === pick.kind
    && state.tag.id === pick.id;
  state.tag = same ? null : pick;
  renderTagFilter();
  document.querySelectorAll(".rule").forEach((row) => {
    row.classList.toggle("picked",
      state.tag !== null && row.dataset.tagId === state.tag.id);
  });
  loadTrades().catch(showError);
}

document.getElementById("only-pending").addEventListener("change", (e) => {
  state.pendingOnly = e.target.checked;
  loadTrades().catch(showError);
});

/* ---------- вкладки ----------
   Раздел живёт в адресе: ссылка на аналитику должна открывать аналитику, а
   «назад» — возвращать на предыдущую вкладку, а не уводить со страницы. */

const TABS = ["home", "analytics", "trades"];

function currentTab() {
  const name = location.hash.replace(/^#\/?/, "");
  return TABS.includes(name) ? name : TABS[0];
}

function showTab(name) {
  for (const panel of document.querySelectorAll("[data-panel]")) {
    panel.hidden = panel.dataset.panel !== name;
  }
  for (const btn of document.querySelectorAll("#tabs button")) {
    if (btn.dataset.tab === name) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  // Графики меряют себя по настоящей ширине контейнера, а у спрятанной вкладки
  // она нулевая: без перерисовки после показа они остаются шириной в ноль.
  redrawCharts();
}

document.getElementById("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) location.hash = "#/" + btn.dataset.tab;
});

window.addEventListener("hashchange", () => showTab(currentTab()));

/* ---------- состояние на сегодня ----------
   Главная отвечает на вопрос «что происходит сейчас», а не «как я торговал
   вообще»: итог дня, итог недели и сколько сделок ждут разбора. */

function plural(n, one, few, many) {
  const ten = n % 10, hundred = n % 100;
  if (ten === 1 && hundred !== 11) return one;
  if (ten >= 2 && ten <= 4 && (hundred < 10 || hundred >= 20)) return few;
  return many;
}

function sumFrom(points, from) {
  let total = 0, n = 0;
  for (const p of points) if (p.at >= from) { total += p.pnl; n += 1; }
  return { total, n };
}

function renderCards(data) {
  const box = document.getElementById("today-cards");
  box.replaceChildren();

  const points = data.series || [];
  const dayStart = startOfDay(Date.now());
  const today = sumFrom(points, dayStart);
  const week = sumFrom(points, dayStart - 6 * 86400000);

  const hero = el("div", "card hero");
  hero.append(el("div", "label", "Сегодня"));
  hero.append(el("div", "value", fmtUsd(today.total) + " USDT"));
  hero.append(el("div", "hint", today.n
    ? today.n + " " + plural(today.n, "сделка закрыта", "сделки закрыты", "сделок закрыто")
    : "сегодня закрытых сделок ещё нет"));
  box.append(hero);

  const seven = el("div", "card");
  seven.append(el("div", "label", "За неделю"));
  seven.append(el("div", "value " + pnlClass(week.total),
                   fmtUsd(week.total) + " USDT"));
  seven.append(el("div", "hint",
    week.n + " " + plural(week.n, "сделка", "сделки", "сделок") + " за 7 дней"));
  box.append(seven);

  // Разобранность берётся из статистики правил: она считает по тем же сделкам
  // периода, что и всё остальное на странице, — своего счётчика заводить незачем.
  const cov = data.rules || {};
  const total = cov.of_total || 0;
  const pending = Math.max(0, total - (cov.reviewed || 0));

  const todo = el("button", "card");
  todo.type = "button";
  todo.append(el("div", "label", "Ждут разбора"));
  todo.append(el("div", "value" + (pending ? " todo" : ""), String(pending)));
  todo.append(el("div", "hint", total
    ? "из " + total + " " + plural(total, "сделки", "сделок", "сделок") + " за период"
    : "закрытых сделок за период нет"));
  todo.addEventListener("click", () => {
    // Счётчик существует ради того, чтобы по нему уйти к делу: ведёт в сделки
    // и сразу ставит отбор «только без разбора».
    state.pendingOnly = true;
    document.getElementById("only-pending").checked = true;
    location.hash = "#/trades";
    loadTrades().catch(showError);
  });
  box.append(todo);
}

/* ---------- календарь P&L ----------
   Считается на клиенте из сырого ряда сделок ровно по той причине, по которой
   сервер этот ряд сырым и отдаёт: границу суток надо проводить в часовом поясе
   того, кто смотрит. Сделка, закрытая в два часа ночи, иначе уезжает во вчера. */

let pnlMonth = null;
let pnlPinned = false;   // месяц выбран руками — сами его больше не двигаем

function dailyTotals() {
  const map = new Map();
  for (const p of state.series || []) {
    const key = startOfDay(p.at);
    const cur = map.get(key) || { pnl: 0, n: 0 };
    cur.pnl += p.pnl;
    cur.n += 1;
    map.set(key, cur);
  }
  return map;
}

function pickPnlDay(year, month, day) {
  const from = new Date(year, month, day).getTime();
  const to = endOfDay(new Date(year, month, day));
  // Повторный клик снимает отбор — тем же движением, каким он ставится.
  const same = state.from === from && state.to === to;
  state.from = same ? null : from;
  state.to = same ? null : to;
  pnlPinned = true;
  applyDates();
  // День выбирают, чтобы посмотреть, что в этот день было: календарь работает
  // навигацией по времени, поэтому сразу уводит к сделкам.
  if (!same) location.hash = "#/trades";
}

function renderPnlCalendar() {
  const box = document.getElementById("pnl-calendar");
  const summary = document.getElementById("pnl-summary");
  box.replaceChildren();
  summary.replaceChildren();

  const totals = dailyTotals();

  if (pnlMonth === null || !pnlPinned) {
    // Открываемся на последнем месяце со сделками, а не на текущем: за период
    // «90 дней» текущий месяц вполне бывает пустым, и календарь показывал бы
    // пустую сетку при непустой статистике рядом.
    const keys = [...totals.keys()];
    const anchor = keys.length ? new Date(Math.max(...keys)) : new Date();
    pnlMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  }

  const year = pnlMonth.getFullYear(), month = pnlMonth.getMonth();
  document.getElementById("pnl-month").textContent = MONTHS[month] + " " + year;

  for (const w of WEEKDAYS) box.append(el("div", "pnl-weekday", w));

  // getDay(): 0 — воскресенье. Неделя здесь начинается с понедельника, как
  // принято локально: иначе выходные оказываются по разные края сетки.
  const lead = (new Date(year, month, 1).getDay() + 6) % 7;
  for (let i = 0; i < lead; i++) box.append(el("div", "pnl-day blank"));

  const days = new Date(year, month + 1, 0).getDate();
  let monthTotal = 0, monthTrades = 0, up = 0, traded = 0;

  for (let d = 1; d <= days; d++) {
    const key = startOfDay(new Date(year, month, d).getTime());
    const cell = totals.get(key);

    // День без сделок — не кнопка и без подписи: тридцать повторов «нет сделок»
    // на месяц читаются как шум, а пустота и так видна по отсутствию цифры.
    if (!cell) {
      const quiet = el("div", "pnl-day flat");
      quiet.append(el("div", "pnl-num", String(d)));
      box.append(quiet);
      continue;
    }

    const btn = el("button", "pnl-day");
    btn.type = "button";
    btn.append(el("div", "pnl-num", String(d)));

    monthTotal += cell.pnl;
    monthTrades += cell.n;
    traded += 1;
    if (cell.pnl > 0) up += 1;

    const trades = cell.n + " " + plural(cell.n, "сделка", "сделки", "сделок");
    btn.classList.add(cell.pnl >= 0 ? "gain" : "loss");
    btn.append(el("div", "pnl-money " + pnlClass(cell.pnl), fmtUsd(cell.pnl)));
    // Число сделок за день биржа не показывает, а без него «−264 за одну» и
    // «−264 за семь» выглядят одинаково, хотя это разные истории.
    btn.append(el("div", "pnl-count", trades));
    btn.setAttribute("aria-label",
      d + " " + MONTHS[month] + ": " + fmtUsd(cell.pnl) + " USDT, " + trades);

    if (state.from === key && state.to === endOfDay(new Date(year, month, d))) {
      btn.classList.add("picked");
    }
    btn.addEventListener("click", () => pickPnlDay(year, month, d));
    box.append(btn);
  }

  // Итог месяца складывается из тех же ячеек, что нарисованы выше. Посчитанный
  // отдельно, он однажды разошёлся бы с сеткой под собой — и поймать это было
  // бы нечем.
  summary.append(el("span", null, "P&L за месяц"));
  summary.append(el("b", pnlClass(monthTotal), fmtUsd(monthTotal)));
  summary.append(el("span", null, "дней в плюсе"));
  summary.append(el("b", null, traded ? up + " из " + traded : "—"));
  summary.append(el("span", null, "сделок"));
  summary.append(el("b", null, String(monthTrades)));
}

for (const [id, step] of [["pnl-prev", -1], ["pnl-next", 1]]) {
  document.getElementById(id).addEventListener("click", () => {
    pnlMonth = new Date(pnlMonth.getFullYear(), pnlMonth.getMonth() + step, 1);
    pnlPinned = true;
    renderPnlCalendar();
  });
}

document.querySelectorAll("#pnl-view button").forEach((btn) => {
  btn.addEventListener("click", () => {
    const asCalendar = btn.dataset.view === "calendar";
    document.querySelectorAll("#pnl-view button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    document.getElementById("pnl-calendar").hidden = !asCalendar;
    document.getElementById("pnl-summary").hidden = !asCalendar;
    document.getElementById("month-nav").hidden = !asCalendar;
    document.getElementById("daily").hidden = asCalendar;
    if (!asCalendar) redrawCharts();
  });
});

/* ---------- боковая панель разбора ---------- */

const drawer = document.getElementById("drawer");
const scrim = document.getElementById("scrim");
const drawerBody = document.getElementById("drawer-body");
const drawerTitle = document.getElementById("drawer-title");
let drawerTrade = null;
let drawerOpener = null;

function refreshDrawer() {
  if (drawerTrade) drawerBody.replaceChildren(detailBody(drawerTrade));
}

function showDrawer(trade, opener) {
  drawerTrade = trade;
  drawerOpener = opener || null;
  drawerTitle.textContent = trade.symbol + " · " + (trade.closed_at === null
    ? "в позиции" : fmtDate(trade.closed_at));
  refreshDrawer();
  scrim.hidden = false;
  drawer.hidden = false;
  const area = drawerBody.querySelector("textarea");
  if (area) area.focus();
}

function hideDrawer() {
  drawer.hidden = true;
  scrim.hidden = true;
  drawerTrade = null;
  // Фокус возвращается туда, откуда панель открыли: иначе после закрытия он
  // с клавиатуры оказывается в начале страницы, и до той же строки надо
  // добираться заново.
  if (drawerOpener && document.body.contains(drawerOpener)) drawerOpener.focus();
  drawerOpener = null;
}

document.getElementById("drawer-close").addEventListener("click", hideDrawer);
scrim.addEventListener("click", hideDrawer);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !drawer.hidden) hideDrawer();
});

showTab(currentTab());
loadAll();
