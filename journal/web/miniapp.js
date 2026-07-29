/* Mini App. Все данные вставляются через textContent — заметки пишет
   пользователь, и они не должны исполняться как разметка. */

"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const state = { days: 30, pendingOnly: false, limit: 20, initData: "", rules: [] };

/* ---------- утилиты ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const fmt = {
  usd(x) {
    if (x === null || x === undefined) return "—";
    return (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(2);
  },
  pct(x) { return Math.round(x * 100) + "%"; },
  date(ms) {
    return new Date(ms).toLocaleString("ru-RU",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  },
  sym(s) { return s.endsWith("USDT") ? s.slice(0, -4) : s; },
};

function cls(x) { return x > 0 ? "pos" : x < 0 ? "neg" : ""; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), "X-Init-Data": state.initData },
  });
  if (response.status === 401) throw new Error("доступ отклонён Telegram");
  if (!response.ok) throw new Error("сервер ответил " + response.status);
  return response.json();
}

function haptic(type) {
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(type);
}

/* ---------- сводка ---------- */

function renderHero(s, note) {
  const hero = document.getElementById("hero");
  hero.replaceChildren();
  if (!s.n) {
    hero.append(el("div", "sub", "Закрытых сделок за период нет."));
    return;
  }
  hero.append(el("div", "label", "Итог за период"));
  hero.append(el("div", "value " + cls(s.total), fmt.usd(s.total) + " USDT"));
  hero.append(el("div", "sub", s.n + " сделок · " + note));
}

function renderFreshness(f) {
  const box = document.getElementById("freshness");
  box.replaceChildren();
  if (!f || !f.stale) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = f.synced_at === null
    ? "Данные ни разу не обновлялись с биржи"
    : "Данные не обновлялись " + Math.round(f.age_hours) + " ч — синхронизация не работает";
}

function tile(label, value, valueClass, hint, warn) {
  const box = el("div", "tile");
  box.append(el("div", "label", label));
  box.append(el("div", "value " + (valueClass || ""), value));
  if (hint) box.append(el("div", "hint" + (warn ? " warn" : ""), hint));
  return box;
}

function renderTiles(data) {
  const box = document.getElementById("tiles");
  const s = data.summary;
  box.replaceChildren();
  if (!s.n) return;

  const cov = data.coverage;
  box.append(tile("Разобрано", cov.annotated + " / " + cov.trades, "",
    "с планом до входа: " + cov.with_intent));

  const [wlo, whi] = s.win_rate_ci;
  box.append(tile("Прибыльных", fmt.pct(s.win_rate), "",
    "95%: " + fmt.pct(wlo) + "–" + fmt.pct(whi)));

  if (s.payoff !== null) {
    const needed = (1 - s.win_rate) / s.win_rate;
    const under = s.payoff < needed;
    box.append(tile("Прибыль / убыток", s.payoff.toFixed(2), under ? "neg" : "pos",
      "нужно " + needed.toFixed(2), under));
  }

  const [elo, ehi] = s.expectancy_ci;
  const zero = elo < 0 && ehi > 0;
  box.append(tile("На сделку", fmt.usd(s.expectancy), cls(s.expectancy),
    zero ? "интервал накрывает ноль" : "95%: " + fmt.usd(elo) + "…" + fmt.usd(ehi),
    zero));
}

/* ---------- правила ----------
   На телефоне правила только читаются и отмечаются нарушениями — заводятся и
   правятся они на маке. Разбор с телефона и так основной сценарий, а набирать
   формулировку правила большим пальцем незачем. */

function renderRules(data) {
  const measured = data.rules;
  const byId = {};
  for (const r of measured.rules) byId[r.rule_id] = r;

  document.getElementById("rules-sub").textContent =
    measured.of_total ? "разобрано " + measured.reviewed + " из " + measured.of_total : "";

  const summary = document.getElementById("rules-summary");
  summary.replaceChildren();
  if (state.rules.length && (measured.violated.n || measured.clean.n)) {
    summary.append(el("div", "rules-cmp",
      "с нарушением " + measured.violated.n + " — средний " +
      fmt.usd(measured.violated.avg) + " · без нарушений " + measured.clean.n +
      " — средний " + fmt.usd(measured.clean.avg)));
    if (!measured.enough) {
      summary.append(el("div", "rules-warn",
        "в группах меньше " + measured.min_n + " сделок — разница пока ничего не доказывает"));
    }
  }

  const box = document.getElementById("rules");
  box.replaceChildren();
  if (!state.rules.length) {
    box.append(el("div", "hint", "Правила заводятся в дневнике на маке."));
    return;
  }
  for (const rule of state.rules) {
    const measuredRule = byId[rule.rule_id];
    if (!rule.active && !measuredRule) continue;
    const row = el("div", "rule" + (rule.active ? "" : " archived"));
    row.append(el("div", "grow", rule.body));
    if (measuredRule) {
      const stat = el("div", "num " + (measuredRule.total < 0 ? "neg" : ""),
        measuredRule.n ? measuredRule.n + " · " + fmt.usd(measuredRule.total) : "—");
      row.append(stat);
    }
    box.append(row);
  }
}

function renderTop(top) {
  const box = document.getElementById("top");
  const sub = document.getElementById("top-sub");
  box.replaceChildren();
  if (!top.trades.length) {
    sub.textContent = "";
    box.append(el("div", "empty", "Прибыльных сделок нет."));
    return;
  }
  sub.textContent = top.share_of_wins !== null
    ? fmt.pct(top.share_of_wins) + " всей прибыли" : "";

  const peak = top.trades[0].net_pnl || 1;
  for (const t of top.trades) {
    const row = el("div", "row");
    const left = el("div", "grow");
    left.append(el("div", "sym", fmt.sym(t.symbol)));
    const bar = el("div", "bar");
    bar.style.width = Math.max(4, t.net_pnl / peak * 100) + "%";
    left.append(bar);
    row.append(left);
    row.append(el("div", "num pos", fmt.usd(t.net_pnl)));
    box.append(row);
  }
}

/* ---------- сделки ---------- */

function editor(trade, row) {
  const wrap = el("div", "editor");
  const area = el("textarea");
  area.value = trade.note || "";
  area.placeholder = "Почему заходил, что увидел, что пошло не так";
  wrap.append(area);

  const active = state.rules.filter((r) => r.active);
  if (active.length) {
    const box = el("div", "violations");
    box.append(el("div", "hint", "Какие правила нарушены:"));
    for (const rule of active) {
      const label = el("label", "violation");
      const check = el("input");
      check.type = "checkbox";
      check.checked = trade.violations.includes(rule.rule_id);
      check.addEventListener("change", async () => {
        check.disabled = true;
        try {
          await api("/api/violation", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              trade_id: trade.trade_id, rule_id: rule.rule_id, broken: check.checked,
            }),
          });
          trade.violations = check.checked
            ? trade.violations.concat(rule.rule_id)
            : trade.violations.filter((id) => id !== rule.rule_id);
          haptic("success");
          loadSummary().catch(() => {});
        } catch (err) {
          check.checked = !check.checked;   // не притворяемся, что сохранилось
          haptic("error");
        } finally {
          check.disabled = false;
        }
      });
      label.append(check, el("span", null, rule.body));
      box.append(label);
    }
    wrap.append(box);
  }

  if (trade.has_intent) {
    wrap.append(el("div", "hint", "План до входа: " + (trade.thesis || "")));
  }
  wrap.append(el("div", "hint",
    "Запись после результата — это память под исход, а не оценка решения."));

  const save = el("button", null, "Сохранить разбор");
  save.type = "button";
  const status = el("div", "status");
  wrap.append(save, status);

  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "…";
    try {
      const payload = await api("/api/note", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: trade.trade_id, body: area.value }),
      });
      trade.note = area.value.trim() || null;
      status.textContent = trade.note ? "сохранено" : "очищено";
      haptic("success");
      row.querySelector(".mark").textContent = trade.note ? "📝" : "";
      loadSummary().catch(() => {});
    } catch (err) {
      status.textContent = "не сохранилось: " + err.message;
      haptic("error");
    } finally {
      save.disabled = false;
    }
  });
  return wrap;
}

function tradeRow(trade) {
  const row = el("div", "row tappable");
  const left = el("div", "grow");
  const head = el("div");
  head.append(el("span", "sym", fmt.sym(trade.symbol)));
  head.append(el("span", "dir", " " + (trade.direction === "long" ? "▲" : "▼")));
  left.append(head);
  left.append(el("div", "meta", fmt.date(trade.closed_at)));
  row.append(left);
  row.append(el("div", "mark", trade.note ? "📝" : ""));
  row.append(el("div", "num " + cls(trade.net_pnl), fmt.usd(trade.net_pnl)));

  let open = null;
  row.addEventListener("click", () => {
    if (open) { open.remove(); open = null; return; }
    open = editor(trade, row);
    row.after(open);
    open.querySelector("textarea").focus();
  });
  return row;
}

function renderTrades(trades) {
  const box = document.getElementById("trades");
  const more = document.getElementById("more");
  box.replaceChildren();

  if (!trades.length) {
    box.append(el("div", "empty",
      state.pendingOnly ? "Всё разобрано." : "Сделок за период нет."));
    more.hidden = true;
    return;
  }
  for (const trade of trades.slice(0, state.limit)) box.append(tradeRow(trade));
  more.hidden = trades.length <= state.limit;
  more.textContent = "Показать ещё " +
    Math.min(20, trades.length - state.limit);
}

/* ---------- загрузка ---------- */

let lastTrades = [];

async function loadSummary() {
  const data = await api("/api/summary?days=" + state.days);
  renderFreshness(data.freshness);
  renderHero(data.summary, data.sample_note);
  renderTiles(data);
  renderRules(data);
  renderTop(data.top_trades);
}

async function loadRules() {
  state.rules = (await api("/api/rules")).rules;
}

async function loadTrades() {
  const data = await api(
    "/api/trades?days=" + state.days + "&pending=" + (state.pendingOnly ? 1 : 0));
  lastTrades = data.trades;
  renderTrades(lastTrades);
}

function fail(message) {
  const gate = document.getElementById("gate");
  gate.className = "gate error";
  gate.textContent = message;
  gate.hidden = false;
  document.getElementById("app").hidden = true;
}

async function loadAll() {
  try {
    // Правила первыми: по ним рисуются и панель, и галочки в разборе.
    await loadRules();
    await Promise.all([loadSummary(), loadTrades()]);
  } catch (err) {
    fail("Не удалось загрузить: " + err.message);
  }
}

/* ---------- запуск ---------- */

document.querySelectorAll(".periods button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.days = Number(btn.dataset.days);
    state.limit = 20;
    document.querySelectorAll(".periods button").forEach(
      (b) => b.removeAttribute("aria-current"));
    btn.setAttribute("aria-current", "true");
    loadAll();
  });
});

document.getElementById("only-pending").addEventListener("change", (e) => {
  state.pendingOnly = e.target.checked;
  state.limit = 20;
  loadTrades().catch((err) => fail(err.message));
});

document.getElementById("more").addEventListener("click", () => {
  state.limit += 20;
  renderTrades(lastTrades);
});

function start() {
  if (tg) {
    tg.ready();
    tg.expand();
    state.initData = tg.initData || "";
  }
  if (!state.initData) {
    fail("Открой это через кнопку в боте — снаружи Telegram данные недоступны.");
    return;
  }
  document.getElementById("gate").hidden = true;
  document.getElementById("app").hidden = false;
  loadAll();
}

start();
