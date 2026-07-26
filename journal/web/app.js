/* Дневник: рендер без фреймворков. Все данные вставляются через textContent —
   заметки пишет пользователь, и они не должны исполняться как разметка. */

"use strict";

const state = { days: 0, pendingOnly: false };

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

/* ---------- KPI ---------- */

function kpi(label, value, opts = {}) {
  const card = el("div", "kpi" + (opts.headline ? " headline" : ""));
  card.append(el("div", "label", label));
  const v = el("div", "value" + (opts.cls ? " " + opts.cls : ""), value);
  card.append(v);
  if (opts.hint) card.append(el("div", "hint" + (opts.warn ? " warn" : ""), opts.hint));
  return card;
}

function renderKpis(data) {
  const s = data.summary;
  const box = document.getElementById("kpis");
  box.replaceChildren();

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

  box.append(kpi("Итог за период", fmtUsd(s.total) + " USDT", {
    cls: pnlClass(s.total),
    hint: s.n + " сделок · " + data.sample_note,
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

  const [elo, ehi] = s.expectancy_ci;
  const crossesZero = elo < 0 && ehi > 0;
  box.append(kpi("Матожидание / сделку", fmtUsd(s.expectancy), {
    cls: pnlClass(s.expectancy),
    hint: crossesZero
      ? "интервал " + fmtUsd(elo) + "…" + fmtUsd(ehi) + " накрывает ноль"
      : "95% интервал " + fmtUsd(elo) + "…" + fmtUsd(ehi),
    warn: crossesZero,
  }));

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

/* ---------- наблюдения ---------- */

function fact(title, body, warn) {
  const wrap = el("div", "fact" + (warn ? " warn" : ""));
  wrap.append(el("dt", null, title));
  wrap.append(el("dd", null, body));
  return wrap;
}

function renderFacts(data) {
  const box = document.getElementById("facts");
  box.replaceChildren();
  const s = data.summary;
  if (!s.n) return;

  const h = data.holding;
  if (h.median_win_hours !== null && h.median_loss_hours !== null) {
    const skew = h.median_loss_hours > h.median_win_hours * 1.5;
    box.append(fact(
      "Медиана удержания",
      "прибыльные " + h.median_win_hours.toFixed(1) + " ч (n=" + h.n_wins + ") · " +
      "убыточные " + h.median_loss_hours.toFixed(1) + " ч (n=" + h.n_losses + ")" +
      (skew ? " — убытки пересиживаются" : ""),
      skew
    ));
  }

  if (!data.r.available) {
    box.append(fact(
      "R-multiple",
      "недоступен: ни у одной из " + data.r.of_total + " сделок нет стопа, " +
      "записанного до входа. Появится после intent --stop.",
      true
    ));
  } else {
    box.append(fact("R-multiple", "посчитан по " + data.r.n + " сделкам"));
  }

  box.append(fact(
    "Валовой P&L",
    fmtUsd(s.gross) + " USDT; издержки изменили итог на " + fmtUsd(-s.costs)
  ));

  if (s.fees_from_exchange) {
    box.append(fact(
      "Комиссия в MNT",
      "у " + s.fees_from_exchange + " сделок комиссия взята из closed-pnl биржи " +
      "и не проверяется независимо"
    ));
  }

  if (s.liquidated) {
    box.append(fact("Ликвидации", String(s.liquidated), true));
  }
}

/* ---------- таблица сделок ---------- */

function detailRow(t) {
  const tr = el("tr", "detail");
  const td = el("td");
  td.colSpan = 7;
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
      const r = await fetch("/api/note", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: t.trade_id, body: area.value }),
      });
      if (!r.ok) throw new Error(String(r.status));
      const payload = await r.json();
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
    td.colSpan = 7;
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
    tr.append(el("td", "num " + pnlClass(t.net_pnl), fmtUsd(t.net_pnl)));

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

async function loadSummary() {
  const data = await getJSON("/api/summary?days=" + state.days);
  renderKpis(data);
  renderTopTrades(data.top_trades);
  renderFacts(data);
}

async function loadTrades() {
  const data = await getJSON(
    "/api/trades?days=" + state.days + "&pending=" + (state.pendingOnly ? 1 : 0));
  renderTrades(data.trades);
}

function loadAll() {
  loadSummary().catch(showError);
  loadTrades().catch(showError);
}

function showError(err) {
  const box = document.getElementById("kpis");
  box.replaceChildren(el("div", "empty", "Ошибка загрузки: " + err.message));
}

document.querySelectorAll(".periods button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.days = Number(btn.dataset.days);
    document.querySelectorAll(".periods button").forEach((b) =>
      b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    loadAll();
  });
});

document.getElementById("only-pending").addEventListener("change", (e) => {
  state.pendingOnly = e.target.checked;
  loadTrades().catch(showError);
});

loadAll();
