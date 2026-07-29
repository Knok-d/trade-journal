/* Дневник: рендер без фреймворков. Все данные вставляются через textContent —
   заметки пишет пользователь, и они не должны исполняться как разметка. */

"use strict";

const state = { days: 0, from: null, to: null, pendingOnly: false,
                tags: { rule: [], reason: [] } };

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
   действует на фронте. Штрихи помечены non-scaling-stroke, поэтому картинка
   тянется по ширине окна, не размазывая линии. */

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 600, H = 150, PAD = 6;

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function canvas() {
  const root = svg("svg", {
    viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  return root;
}

function emptyChart(box, text) {
  box.replaceChildren(el("div", "empty", text));
}

function renderEquity(points) {
  const box = document.getElementById("equity");
  const sub = document.getElementById("equity-sub");
  if (points.length < 2) {
    sub.textContent = "";
    emptyChart(box, "Сделок за период недостаточно для кривой.");
    return;
  }

  const xs = points.map((p) => p.at);
  const ys = points.map((p) => p.cum);
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = hi - lo || 1;
  const px = (t) => PAD + (x1 === x0 ? 0 : (t - x0) / (x1 - x0)) * (W - 2 * PAD);
  const py = (v) => H - PAD - (v - lo) / span * (H - 2 * PAD);

  const root = canvas();
  // Ноль подписан линией: без него подъём с −900 до −800 выглядит прибылью.
  root.append(svg("line", {
    x1: PAD, x2: W - PAD, y1: py(0), y2: py(0),
    class: "axis", "vector-effect": "non-scaling-stroke",
  }));

  const d = points.map((p, i) => (i ? "L" : "M") + px(p.at) + " " + py(p.cum)).join(" ");
  const last = ys[ys.length - 1];
  root.append(svg("path", {
    d: d + ` L ${px(x1)} ${py(0)} L ${px(x0)} ${py(0)} Z`,
    class: "area " + (last >= 0 ? "pos" : "neg"),
  }));
  root.append(svg("path", {
    d, class: "line " + (last >= 0 ? "pos" : "neg"),
    "vector-effect": "non-scaling-stroke",
  }));
  root.setAttribute("aria-label",
    "Накопленный результат: " + fmtUsd(last) + " USDT за " + points.length + " сделок");

  box.replaceChildren(root);
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
  if (!points.length) {
    sub.textContent = "";
    emptyChart(box, "Сделок за период нет.");
    return;
  }

  // Сутки режутся по местному времени, а не по UTC: сделка в два часа ночи
  // принадлежит этой ночи, а не вчерашнему дню сервера.
  const byDay = new Map();
  for (const p of points) {
    const d = new Date(p.at);
    const key = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    byDay.set(key, (byDay.get(key) || 0) + p.pnl);
  }
  const days = [...byDay.entries()].sort((a, b) => a[0] - b[0]);
  const peak = Math.max(...days.map(([, v]) => Math.abs(v))) || 1;
  const step = (W - 2 * PAD) / days.length;
  const mid = H / 2;

  const root = canvas();
  root.append(svg("line", {
    x1: PAD, x2: W - PAD, y1: mid, y2: mid,
    class: "axis", "vector-effect": "non-scaling-stroke",
  }));
  days.forEach(([day, value], i) => {
    const height = Math.abs(value) / peak * (mid - PAD);
    const width = Math.max(1, step * 0.7);
    root.append(svg("rect", {
      x: PAD + i * step + (step - width) / 2,
      y: value >= 0 ? mid - height : mid,
      width, height: Math.max(1, height),
      class: value >= 0 ? "bar pos" : "bar neg",
    }));
  });
  root.setAttribute("aria-label", "Результат по дням, " + days.length + " дней");

  box.replaceChildren(root);
  const wins = days.filter(([, v]) => v > 0).length;
  sub.textContent = days.length + " дней · в плюс " + wins;
}

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
  renderEquity(data.series);
  renderDaily(data.series);
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
