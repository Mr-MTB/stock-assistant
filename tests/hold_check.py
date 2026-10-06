"""Offline check of the hold-longer study (research.py research_hold). No network.

1. The study's copy of the bot's current rules (v2) must make exactly the same trades as the live bot's
   own backtest (main's daytrader.simulate_day) on the same prices.
2. Hand-made trades: a stop-out that later recovers and reaches its target, and a 2-hour exit that keeps
   falling, must get the right 'held longer' results.
3. The live-trade check reads prices after the sale correctly.
4. The full study runs and writes its report.

Run from the repo root (needs main's daytrader.py saved as tests/daytrader_v2.py):
    git show main:daytrader.py > tests/daytrader_v2.py && python3 tests/hold_check.py
"""
import datetime as dt
import importlib.util
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake as F  # noqa: E402

D = F.D
import research as R  # noqa: E402

spec = importlib.util.spec_from_file_location("daytrader_v2", os.path.join(HERE, "daytrader_v2.py"))
V2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V2)

NY = D.NY
SYMS = ["NVDA", "AAPL", "MSFT", "AMD", "KO", "XOM", "PEP", "INTC"]
D.WATCHLIST = V2.WATCHLIST = SYMS
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


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


os.chdir(tempfile.mkdtemp(prefix="hold-"))
days = [d for d in (dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(70)) if d.weekday() < 5][:45]
world = MixedWorld(days, SYMS + ["SPY"], seed=7)
F.install(world, dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY))
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)
V2.api = F.fake_api

# 1. Same trades as the live bot's backtest
cal = D.calendar(days[0], days[-1])
daily = V2.get_bars(SYMS + ["SPY"], "1Day", cal[0][1] - dt.timedelta(days=60), cal[-1][2], adjustment="split")
bot = []
for day, o, c in cal:
    bars = V2.get_bars(SYMS + ["SPY"], "1Min", o, c)
    trades, _, _ = V2.simulate_day(bars, V2.stats_for_day(daily, day), 533.33, day, o, c)
    bot += trades
symbols = R.syms()
raw = R.load_days(cal, symbols)
table = R.daily_table(symbols, cal)
feats = [R.Day(day, raw[day], R.day_stats(table, symbols, day)) for day, _, _ in cal]
mine = [t for F_ in feats for t in R.v2_day(F_)]
check("v2: same number of trades as the bot's backtest", len(bot) == len(mine), (len(bot), len(mine)))
check("v2: enough trades to mean something", len(bot) >= 30, len(bot))
check("v2: covers target, stop and time exits", {t["exit_reason"] for t in bot} == {"target", "stop", "time"},
      {t["exit_reason"] for t in bot})
key = lambda t: (t["date"], t["symbol"])  # noqa: E731
mismatch = []
for a, b in zip(sorted(bot, key=key), sorted(mine, key=lambda t: (t["day"].isoformat(), symbols[t["stock"]]))):
    same = (a["date"] == b["day"].isoformat() and a["symbol"] == symbols[b["stock"]]
            and abs(a["entry"] - b["entry"]) < 0.006 and abs(a["exit"] - b["exit"]) < 0.006
            and a["exit_reason"] == b["why"] and abs(a["pct_of_account"] - b["ret"]) < 1e-4)
    if not same:
        mismatch.append((a, {k: b[k] for k in ("day", "stock", "entry", "exit", "why", "ret")}))
check("v2: every trade identical (day, stock, entry, exit, reason, result)", not mismatch, mismatch[:2])

# 2. Hand-made paths
ohlc = R.daily_ohlc(symbols, feats)
i0 = 10
F0 = feats[i0]
s0 = symbols.index("NVDA")
F0.o[s0, :] = F0.h[s0, :] = F0.l[s0, :] = F0.c[s0, :] = 100.0
F0.l[s0, 30] = 98.0                       # stop hit at minute 30
F0.h[s0, 200] = F0.c[s0, 200] = 103.0     # later that day: back up past the target
t_stop = {"stock": s0, "minute": 19, "entry": 100.0, "target": 102.0, "stop": 99.0, "why": "stop", "k_exit": 30,
          "ret": -0.01, "size": 1 / 3}
res, back = R.hold_outcomes(feats, i0, t_stop, ohlc, "NVDA")
check("stop-out that recovers: no stop + hold to close reaches the target",
      abs(res["close"] - R.net(100.0, 102.0)) < 1e-9, res)
check("stop-out that recovers: 2-hour rule without stop ends flat (target came later)",
      abs(res["2h"] - R.net(100.0, 100.0)) < 1e-9, res)
check("stop-out that recovers: came back above break-even later that day, not within 2 hours",
      back["close"] is True and back["2h"] is False, back)
check("stop-out that recovers: multi-day rules take the same-day target", res[1] == res[3] == res[5] == res["close"], res)

F1 = feats[i0 + 1]
s1 = symbols.index("AAPL")
F1.o[s1, :] = F1.h[s1, :] = F1.l[s1, :] = F1.c[s1, :] = np.linspace(100, 95, F1.n)   # falls all day
t_time = {"stock": s1, "minute": 19, "entry": 100.0, "target": 103.0, "stop": 98.5, "why": "time",
          "k_exit": 140, "ret": -0.004, "size": 1 / 3}
res, back = R.hold_outcomes(feats, i0 + 1, t_time, ohlc, "AAPL")
check("2-hour loser that keeps falling: holding to the close loses more", res["close"] < res["2h"] < 0, res)
check("2-hour loser that keeps falling: never back above break-even that day", back["close"] is False, back)
check("end of data: horizons beyond the last day are unknown",
      R.hold_outcomes(feats, len(feats) - 2, dict(t_time, stock=s1, target=1e9), ohlc, "AAPL")[0][3] is None)

# 3. Live trades: what happened after the sale
live_day = days[-1]
world.now = dt.datetime.combine(live_day, dt.time(16, 30), NY)
D.now_ny = lambda: world.now
sold = world.minute[("MSFT", live_day)][60]
row = {"date": live_day.isoformat(), "symbol": "MSFT", "entry": f"{sold['c'] * 1.01:.4f}", "exit": f"{sold['c']:.4f}",
       "target": f"{sold['c'] * 1.03:.4f}", "exit_time_ny": R.clock12(sold["t"]), "exit_reason": "stop",
       "pnl_pct": "-1.2", "result": "loss"}
lines = R.live_after_sale([row])
after = [b for b in world.minute[("MSFT", live_day)] if b["t"] >= sold["t"]]
check("live: reports the highest price after the sale",
      any(f"highest ${max(b['h'] for b in after):.2f}" in x for x in lines), lines)
check("live: reports the price at the close", any(f"4:00 PM): ${after[-1]['c']:.2f}" in x for x in lines), lines)

# 3b. Hold-until-close test: its copy of today's rules must give the same trades as v2_day
same, total = 0, 0
for F_ in feats:
    a = R.v2_day(F_)
    b = R.exits_day(F_, 15, 265, R.NOW_RULE, None)
    total += 1
    same += [(t["stock"], round(t["entry"], 6), round(t["ret"] / t["size"], 9)) for t in a] == \
            [(t["stock"], round(t["entry"], 6), round(t["net"], 9)) for t in b]
check("exits: its 'bot today' version matches v2_day on every day", same == total, f"{same}/{total}")

# 3c. Hand-made exit paths: break-even move and trailing stop
F2 = feats[i0 + 2]
s2 = symbols.index("MSFT")
path = [100.0] * F2.n
path[25:30] = [100.5, 101.2, 101.5, 101.0, 100.4]   # +1.5R, then back down
path[30:] = [99.5] * (F2.n - 30)
for k in range(F2.n):  # each minute opens at the previous close, so prices move through levels, not past them
    F2.o[s2, k] = path[k - 1] if k else path[0]
    F2.c[s2, k] = path[k]
    F2.h[s2, k], F2.l[s2, k] = max(F2.o[s2, k], path[k]), min(F2.o[s2, k], path[k])
raw, why = R.walk_exit(F2, s2, 20, 100.0, 99.0, 103.0, F2.n - 5, be_at=1)
check("exits: break-even move turns a stop-out into a scratch", why == "protect" and
      abs(raw - R.break_even(100.0)) < 1e-9, (raw, why))
raw, why = R.walk_exit(F2, s2, 20, 100.0, 99.0, None, F2.n - 5, trail=1)
check("exits: trailing stop follows the best price (101.5 - 1R = 100.5)", why == "protect" and
      abs(raw - 100.5) < 1e-9, (raw, why))
raw, why = R.walk_exit(F2, s2, 20, 100.0, 99.6, None, F2.n - 5)
check("exits: plain stop is hit later", why == "stop" and abs(raw - 99.6) < 1e-9, (raw, why))
raw, why = R.walk_exit(F2, s2, 20, 100.0, None, None, F2.n - 40)
check("exits: no stop, no target: sold at the end time", why == "end" and abs(raw - 99.5) < 1e-9, (raw, why))

# 4. Full study
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)
R.DAYS = len(days)
F.SENT.clear()
R.hold_research(live_rows=[row])
path = f"reports/research-hold-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("study: report has every section", all(k in text for k in (
    "Today's live paper trades", "The past year", "came back above break-even", "If the bot had kept",
    "Fair test", "Bot's rules now")), text[:500])
check("study: Telegram note sent", any("Hold-longer study finished" in m for m in F.SENT), F.SENT[-1:])

# 5. Hold-until-close test runs end to end
F.SENT.clear()
R.exits_research()
path = f"reports/research-exits-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("exits: report has every section", all(k in text for k in (
    "The bot today", "Hold-until-close versions", "Highest win rate", "Every version")), text[:400])
check("exits: every version listed", text.count(": ") and
      sum(1 for x in text.splitlines() if x.startswith(("15-min", "5-min")) and "|" in x) == len(R.exit_versions()),
      len(R.exit_versions()))

print("\nALL PASSED" if ok_all else "\nSOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
