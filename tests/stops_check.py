"""Offline check of the farther-stop study (research.py research_stops). No network.

1. With the bot's own stop, the study makes exactly the same trades as the current rules (v3_breakouts).
2. Every stop sits below the buy price, a farther stop never sells a trade that a closer one keeps,
   and "no stop" only ever sells at 3:20 PM.
3. The live-trade review lists every live trade with each stop, and its "now" column follows the bot's stop.
4. The full study runs and writes its report.

Run from the repo root:  python3 tests/stops_check.py
"""
import datetime as dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
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


os.chdir(tempfile.mkdtemp(prefix="stops-"))
days = [d for d in (dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(60)) if d.weekday() < 5][:40]
world = MixedWorld(days, SYMS + ["SPY"], seed=7)
F.install(world, dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY))
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)

cal = D.calendar(days[0], days[-1])
symbols = R.syms()
raw = R.load_days(cal, symbols)
table = R.daily_table(symbols, cal)
feats = [R.Day(day, raw[day], R.day_stats(table, symbols, day)) for day, _, _ in cal]
atrs = R.atr_table(symbols, cal[0][1] - dt.timedelta(days=60), cal[-1][2])


def atr_of(F_, s):
    return R.atr_before(atrs, symbols[s], F_.day)


# 1. The bot's own stop gives the bot's own trades
same, total, n = 0, 0, 0
for F_ in feats:
    a = [(r["s"], round(r["net"], 9)) for r in R.take_first(R.v3_breakouts(F_), lambda r: True)]
    b = [(t["s"], round(t["net"], 9)) for t in R.stop_day(F_, R.STOP_RULES[0], atr_of)]
    same += a == b
    total += 1
    n += len(b)
check("stops: the bot's own stop gives exactly the current rules' trades", same == total, f"{same}/{total}")
check("stops: enough trades to mean something", n >= 30, n)

# 2. Sensible stops
rule = {r[1]: r for r in R.STOP_RULES}
below, monotone, nostop, buys_same = True, True, True, True
for F_ in feats:
    by = {k: R.stop_day(F_, rule[k], atr_of) for k in ("now", "1%", "2%", "3%", "no stop", "1 ATR")}
    buys_same &= len({tuple(t["s"] for t in v) for v in by.values()}) == 1
    for k, trades in by.items():
        for t in trades:
            if t["stop"] is not None:
                below &= t["stop"] < t["entry"]
    for t1, t2, t3 in zip(by["1%"], by["2%"], by["3%"]):
        monotone &= (t3["why"] != "stop" or t2["why"] == "stop") and (t2["why"] != "stop" or t1["why"] == "stop")
    nostop &= all(t["why"] == "end" for t in by["no stop"])
check("stops: every stop is below the buy price", below)
check("stops: the same stocks are bought whatever the stop", buys_same)
check("stops: a farther stop never sells a trade that a closer one keeps", monotone)
check("stops: 'no stop' only sells at 3:20 PM", nostop)
a = atr_of(feats[10], 0)
check("stops: a normal day's move (ATR) looks sensible", 0.001 < a < 0.2, a)
late = R.atr_before(atrs, symbols[0], feats[10].day)
check("stops: ATR uses only days before the trade",
      late == R.atr_before({symbols[0]: (atrs[symbols[0]][0][:R.bisect.bisect_left(atrs[symbols[0]][0], feats[10].day)],
                                         atrs[symbols[0]][1][:R.bisect.bisect_left(atrs[symbols[0]][0], feats[10].day)])},
                           symbols[0], feats[10].day))

# 3. Live-trade review
R.V3_LIVE_START = days[0]
rows = []
for F_ in feats[5:15]:
    for t in R.stop_day(F_, R.STOP_RULES[0], atr_of)[:1]:
        when = dt.datetime.combine(F_.day, dt.time(9, 30)) + dt.timedelta(minutes=t["k0"])
        rows.append({"date": F_.day.isoformat(), "symbol": symbols[t["s"]],
                     "entry_time_ny": when.strftime("%I:%M %p").lstrip("0"), "entry": f"{t['entry']:.4f}",
                     "invested_usd": "176.0", "pnl_pct": f"{t['net'] * 100:.4f}",
                     "exit_reason": "stop" if t["why"] == "stop" else "time"})
lines = R.live_stop_review(rows, feats, symbols, atr_of)
check("live review: one line per live trade, plus header, total and note", len(lines) == len(rows) + 3,
      (len(lines), len(rows)))
cols_ok = all(abs(float(x.split("|")[0].split()[-1].rstrip("s")) - float(x.split("|")[1].strip().rstrip("s"))) < 0.02
              for x in lines[1:-2])
check("live review: the 'now' column matches what the bot's stop did", cols_ok, lines[1:4])
check("live review: every stop has a column", all(r[1] in lines[0] for r in R.STOP_RULES), lines[0])

# 4. Full study
F.SENT.clear()
R.DAYS = len(days)
R.stops_research(live_rows=rows)
path = f"reports/research-stops-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("study: report has every section", all(k in text for k in (
    "Your live trades since", "Each stop over the past year", "BOTH the older and the newer",
    "Made money in BOTH", "last 30 days")), text[:400])
check("study: every stop listed", all(r[0] in text for r in R.STOP_RULES))
check("study: Telegram note sent", any("Farther-stop test finished" in m for m in F.SENT), F.SENT[-1:])

print("\nALL PASSED" if ok_all else "\nSOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
