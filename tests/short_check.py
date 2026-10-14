"""Offline check of the selling-short study (research.py research_short). No network.

1. A hand-made day: a stock that is down and breaks below its first-5-minute low gives one short sale at the
   right minute; it is bought back at 3:20 PM, or at the stop if the price jumps back up; no short sales while
   the S&P 500 is up more than 1%.
2. The money math of a short sale (slippage and Sahm fees on both orders).
3. "Do the opposite of the bot" sells exactly the bot's own buys, and buying + selling the same trade loses about
   twice the costs (so the opposite of a losing strategy is not automatically a winner).
4. The full study runs and writes its report.

Run from the repo root:  python3 tests/short_check.py
"""
import datetime as dt
import os
import sys
import tempfile

import numpy as np

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


# 1. Hand-made day: stock 0 is down, stock 1 is flat, row 2 is the S&P 500.
def make_day(spy_up=0.0, spike_at=None):
    n = 390
    arr = np.zeros((3, n, 5))
    path = np.full(n, 99.2)
    path[:5] = [99.5, 99.3, 99.1, 99.4, 99.2]           # first 5 minutes: high 99.6, low 99.0
    path[5:20] = 99.2
    path[20] = 98.9                                       # closes below the 5-minute low: signal
    path[21:] = np.linspace(98.85, 98.0, n - 21)          # keeps falling
    opens = np.r_[99.5, path[:-1]]
    highs, lows = np.maximum(opens, path) + 0.05, np.minimum(opens, path) - 0.05
    highs[0], lows[2] = 99.6, 99.0
    if spike_at is not None:
        highs[spike_at] = 99.7                            # a jump back above the top of the range
    arr[0] = np.c_[opens, highs, lows, path, np.r_[np.full(5, 50_000), np.full(n - 5, 5_000)]]
    arr[1] = np.c_[np.full(n, 50.0), np.full(n, 50.02), np.full(n, 49.98), np.full(n, 50.0), np.full(n, 1_000)]
    spy = 500 * (1 + spy_up)
    arr[2] = np.c_[np.full(n, spy), np.full(n, spy + 0.1), np.full(n, spy - 0.1), np.full(n, spy), np.full(n, 9_000)]
    stats = np.array([[100.0, 1_000_000, 100, 100, 50], [50.0, 1_000_000, 50, 50, 50], [500.0, 9e6, 500, 500, 50]])
    return R.Day(dt.date(2026, 10, 1), arr, stats)


p = {"or": R.V3_OR, "until": R.V3_UNTIL, "hold": R.V3_BEFORE + 25, "top": 5}
Fd = make_day()
check("short: the falling stock's break below its 5-minute low is the only signal",
      [(m, s) for m, _, s, hi, lo in R.breakdown(Fd, p)] == [(20, 0)], R.breakdown(Fd, p))
t = R.short_day(Fd, "breakdown", "top")
end = Fd.n - R.V3_BEFORE
check("short: sold at the next minute, bought back at 3:20 PM, a win",
      len(t) == 1 and t[0]["k0"] == 21 and t[0]["why"] == "end" and abs(t[0]["raw"] - Fd.o[0, end]) < 1e-9
      and t[0]["net"] > 0, t)
t = R.short_day(make_day(spike_at=60), "breakdown", "top")
check("short: a jump back to the top of the range buys it back at the stop",
      len(t) == 1 and t[0]["why"] == "stop" and abs(t[0]["raw"] - 99.6) < 1e-9 and t[0]["net"] < 0, t)
t = R.short_day(make_day(spike_at=60), "breakdown", "mid")
check("short: the middle stop is hit sooner", len(t) == 1 and t[0]["why"] == "stop" and t[0]["stop"] < 99.6, t)
t = R.short_day(make_day(spike_at=60), "breakdown", None)
check("short: with no stop, only the 3:20 PM buy-back", len(t) == 1 and t[0]["why"] == "end", t)
check("short: no short sales while the S&P 500 is up more than 1%", not R.breakdown(make_day(spy_up=0.012), p))

# 2. Money math
x = 99.0 * (1 + R.SLIP)
expect = (1 - x / 100.0) - R.FEE - R.FEE * x / 100.0
check("short: result after slippage and fees", abs(R.net_short(100.0, 99.0) - expect) < 1e-12)
check("short: a flat trade loses the costs", R.net_short(100.0, 100.0) < -0.002)

# 3. Opposite of the bot, on a fake market
os.chdir(tempfile.mkdtemp(prefix="short-"))
days = [d for d in (dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(60)) if d.weekday() < 5][:40]


class MixedWorld(F.World):
    """Run-ups, failed breakouts, flat days, chop, and the same moves mirrored downwards."""

    def make_day(self, s, d, prev, scenario):
        kind = F.random.choice(["up", "fade", "flat", "chop", "down", "down-fade"])
        base = kind.replace("down-", "").replace("down", "up")
        bars = super().make_day(s, d, prev, lambda *_: "chop" if base == "chop" else "up")
        for i, b in enumerate(bars):
            if base == "fade" and i >= 20:
                f = 1 - 0.002 * (i - 19) if i < 40 else 1 - 0.04
                for k in "ohlc":
                    b[k] *= f
            elif base == "flat" and i > 18:
                level = bars[18]["c"] * (1 + 0.0003 * ((i % 3) - 1))
                for k in "ohlc":
                    b[k] = level
        if kind.startswith("down"):  # mirror image around yesterday's close
            for b in bars:
                b["o"], b["h"], b["l"], b["c"] = prev ** 2 / b["o"], prev ** 2 / b["l"], prev ** 2 / b["h"], prev ** 2 / b["c"]
        return bars


world = MixedWorld(days, SYMS + ["SPY"], seed=7)
F.install(world, dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY))
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)
cal = D.calendar(days[0], days[-1])
symbols = R.syms()
raw = R.load_days(cal, symbols)
table = R.daily_table(symbols, cal)
feats = [R.Day(day, raw[day], R.day_stats(table, symbols, day)) for day, _, _ in cal]
same, pair_ok, n_long, n_short = True, True, 0, 0
for F_ in feats:
    longs = R.stop_day(F_, R.STOP_RULES[-1], lambda *_: 0.02)          # buying, no stop
    shorts = R.short_day(F_, "opposite", None)                         # selling the same, no stop
    same &= [(t["s"], t["k0"]) for t in longs] == [(t["s"], t["k0"]) for t in shorts]
    for a, b in zip(longs, shorts):
        pair_ok &= abs((a["net"] + b["net"]) + 4 * R.SLIP + 4 * R.FEE) < 0.001
    n_long += len(longs)
    n_short += len(R.short_day(F_, "breakdown", "top"))
check("opposite: sells exactly the bot's own buys, at the same minute", same and n_long > 20, n_long)
check("opposite: buying + selling the same trade loses about twice the costs (0.62%)", pair_ok)
check("breakdown: the fake market has falling stocks to sell", n_short > 10, n_short)

# 4. Full study
rows = [{"date": days[-3].isoformat(), "symbol": "NVDA", "pnl_usd": "-1.5"},
        {"date": days[-2].isoformat(), "symbol": "AAPL", "pnl_usd": "2.0"}]
R.LIVE_START = days[-5]
R.DAYS = len(days)
F.SENT.clear()
R.short_research(live_rows=rows)
path = f"reports/research-short-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("study: report has every section", all(k in text for k in (
    "The last days you've seen", "The bot's real paper trades (buying): +1.9 SAR (2 trades, 1 wins)",
    "Day by day", "Each way over the past year", "BOTH the older and the newer", "Made money in BOTH")), text[:600])
check("study: every way listed", all(w[0] in text for w in R.SHORT_WAYS))
check("study: Telegram note sent", any("Selling-short test finished" in m for m in F.SENT), F.SENT[-1:])

print("\nALL PASSED" if ok_all else "\nSOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
