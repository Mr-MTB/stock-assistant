"""Offline check of research.py on fake data (no network).

1. The research engine's copy of the current rules must make exactly the same trades as
   the bot's own backtest (daytrader.simulate_day) on the same prices.
2. The full research run must finish and write its report.

Run from the repo root:  python3 tests/research_check.py
"""
import datetime as dt
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake as F  # noqa: E402

D = F.D
import research as R  # noqa: E402

NY = D.NY
SYMS = ["NVDA", "AAPL", "MSFT", "AMD", "KO", "XOM", "PEP", "INTC"]
D.WATCHLIST = SYMS
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


os.chdir(tempfile.mkdtemp(prefix="research-"))
days = [d for d in (dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(60)) if d.weekday() < 5][:40]


class MixedWorld(F.World):
    """Each stock-day is a clean run-up, a breakout that fails, a breakout that goes flat, or chop."""

    def make_day(self, s, d, prev, scenario):
        kind = F.random.choice(["up", "fade", "flat", "chop"])
        bars = super().make_day(s, d, prev, lambda *_: "chop" if kind == "chop" else "up")
        for i, b in enumerate(bars):
            if kind == "fade" and i >= 20:
                f = 1 - 0.002 * (i - 19) if i < 40 else 1 - 0.04
                for k in "ohlc":
                    b[k] *= f
            elif kind == "flat" and i > 18:
                level = bars[18]["c"] * (1 + 0.0003 * ((i % 3) - 1))
                for k in "ohlc":
                    b[k] = level
        return bars


world = MixedWorld(days, SYMS + ["SPY"], seed=7)
F.install(world, dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY))
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)

# 1. Same trades as the bot's backtest
cal = D.calendar(days[0], days[-1])
daily = D.get_bars(SYMS + ["SPY"], "1Day", cal[0][1] - dt.timedelta(days=45), cal[-1][2], adjustment="split")
balance, bot = 266.67, []
for day, o, c in cal:
    bars = D.get_bars(SYMS, "1Min", o, min(o + dt.timedelta(hours=4, minutes=30), c))
    trades, balance, _ = D.simulate_day(bars, D.stats_for_day(daily, day), balance, day, o, c)
    bot += trades

symbols = R.syms()
raw = R.load_days(cal, symbols)
table = R.daily_table(symbols, cal)
feats = [R.Day(day, raw[day], R.day_stats(table, symbols, day)) for day, _, _ in cal]
mine = R.run(feats, *R.CURRENT)

check("same number of trades", len(bot) == len(mine), (len(bot), len(mine)))
check("enough trades to mean something", len(bot) >= 15, len(bot))
reasons = {t["exit_reason"] for t in bot}
check("covers target, stop and time exits", reasons == {"target", "stop", "time"}, reasons)
mismatch = []
for a, b in zip(bot, mine):
    same = (a["date"] == b["day"].isoformat() and a["symbol"] == symbols[b["stock"]]
            and abs(a["entry"] - b["entry"]) < 0.006 and abs(a["exit"] - b["exit"]) < 0.006
            and a["exit_reason"] == b["why"] and abs(a["pct_of_account"] - b["ret"]) < 1e-4)
    if not same:
        mismatch.append((a, {k: b[k] for k in ("day", "stock", "entry", "exit", "why", "ret")}))
check("every trade identical (day, stock, entry, exit, reason, result)", not mismatch, mismatch[:2])

# 2. Cached prices give the same answer as fresh ones
again = R.load_days(cal, symbols)
check("cached prices identical", all((again[d] == raw[d]).all() for d in raw))

# 3. Full research run writes a report
R.DAYS, R.MIN_TRADES = len(days), 5
R.research()
path = f"reports/research-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("report saved", bool(text), path)
check("report has every section",
      all(k in text for k in ("Where the current rules lose", "Rules with 80%+ winners",
                              "Most profitable rules in the tuning months", "Test months")), text[:300])
check("Telegram note sent", any("Research finished" in m for m in F.SENT), F.SENT[-1:] if F.SENT else None)

# 4. AI filter: the daily pick rule
rows = [{"day": 1, "minute": 10, "stock": 0}, {"day": 1, "minute": 12, "stock": 0},
        {"day": 1, "minute": 20, "stock": 1}, {"day": 1, "minute": 30, "stock": 2},
        {"day": 2, "minute": 5, "stock": 3}, {"day": 2, "minute": 6, "stock": 4}]
scores = [0.9, 0.95, 0.4, 0.8, 0.7, 0.99]
check("pick: time order, bar respected, 2 a day, no repeated stock",
      R.pick(rows, scores, 0.6) == [0, 3, 4, 5], R.pick(rows, scores, 0.6))

# 5. AI filter: what the AI sees never depends on prices after the signal
day0, o0, c0 = cal[5]
stats0 = R.day_stats(table, symbols, day0)
base = R.Day(day0, raw[day0].copy(), stats0)
changed = raw[day0].copy()
m0 = 40
changed[:, m0 + 1:, :] *= 1.07
moved = R.Day(day0, changed, stats0)
t0 = {"entry": 100.0, "stop": 99.0, "target": 101.0}
same = all(np.allclose(R.setup_features(base, s, m0, 3, t0), R.setup_features(moved, s, m0, 3, t0), equal_nan=True)
           for s in range(len(SYMS)))
check("AI features use only information up to the signal", same)

# 6. AI filter study runs end to end
R.MIN_PICKS = 3
F.SENT.clear()
R.ai_research()
path = f"reports/research-ai-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("AI report saved with every section",
      all(k in text for k in ("Choosing the confidence bar", "Result on the test months", "No filter",
                              "AI filter:", "Goal (80%+ winners and profit)")), text[:400])
check("AI Telegram note sent", any("AI filter test finished" in m for m in F.SENT), F.SENT[-1:])
picked = [ln for ln in text.splitlines() if "AI confidence" in ln]
per_day = {}
for ln in picked:
    per_day[ln[:10]] = per_day.get(ln[:10], 0) + 1
check("AI never picks more than 2 a day", all(n <= 2 for n in per_day.values()), per_day)

print("\nALL PASSED" if ok_all else "\nSOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
