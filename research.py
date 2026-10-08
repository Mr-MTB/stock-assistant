"""
Research mode: tests many rule variations on about a year of 1-minute data and reports
which ones reach a high win rate AND make money after Sahm fees.

Honesty check: rules are chosen using only the older 70% of days ("tuning months"), then
judged on the newest 30% ("test months"), which are never used to choose anything.

Run on GitHub: Actions -> Day Trader (paper) -> Run workflow -> mode: research
Output: reports/research-<date>.txt and a short Telegram note.
Downloaded prices are cached in data/ on GitHub (never committed).
"""
import bisect
import concurrent.futures as cf
import csv
import datetime as dt
import io
import itertools
import json
import os
import re
import sys
import time
import urllib.request

import numpy as np

import daytrader as D

np.seterr(all="ignore")  # stocks with missing data give NaN comparisons, which count as "no signal"

DAYS = int(os.environ.get("RESEARCH_DAYS", "250"))
TEST_SHARE = 0.30
MIN_TRADES = 40            # in the tuning months
GOAL_WIN_RATE = 0.80
LOW_SLIP = 0.0001           # 'what if trading were nearly free' comparison
DATA_DIR = "data"
FEE, SLIP, RISK, HOLD = D.COMMISSION_PCT, D.SLIPPAGE, D.RISK_PER_TRADE, D.MAX_HOLD_MIN
MAX_STOP = D.MAX_STOP_PCT
O, H, L, C, V = range(5)


def syms():
    return list(D.WATCHLIST) + ["SPY"]


# ---------------- data ----------------
def fetch_day(symbols, open_t, close_t):
    """Dense (symbols, minutes, 5) array of o,h,l,c,v; NaN where a minute had no trade."""
    n = int((close_t - open_t).total_seconds() // 60)
    arr = np.full((len(symbols), n, 5), np.nan)
    idx = {s: i for i, s in enumerate(symbols)}
    params = {"symbols": ",".join(symbols), "timeframe": "1Min", "start": D.iso(open_t),
              "end": D.iso(close_t - dt.timedelta(seconds=1)), "feed": "iex", "limit": 10000,
              "adjustment": "split"}
    while True:
        r = D.api("GET", D.DATA_URL + "/v2/stocks/bars", params)
        for s, bars in (r.get("bars") or {}).items():
            i = idx.get(s)
            if i is None:
                continue
            for b in bars:
                t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                m = int((t - open_t).total_seconds() // 60)
                if 0 <= m < n:
                    arr[i, m] = (b["o"], b["h"], b["l"], b["c"], b["v"])
        token = r.get("next_page_token")
        if not token:
            return arr
        params = dict(params, page_token=token)


def load_days(days, symbols):
    """{date: array}, downloading only days not already cached in data/."""
    os.makedirs(DATA_DIR, exist_ok=True)
    key = ",".join(symbols)
    out, todo = {}, []
    for day, o, c in days:
        path = os.path.join(DATA_DIR, f"{day}.npz")
        if os.path.exists(path):
            z = np.load(path)
            if str(z["symbols"]) == key:
                out[day] = z["arr"].astype(float)
                continue
        todo.append((day, o, c, path))
    print(f"Prices: {len(out)} days cached, downloading {len(todo)}.")

    def get(job):
        day, o, c, path = job
        arr = fetch_day(symbols, o, c).astype(np.float32)
        np.savez_compressed(path, arr=arr, symbols=np.array(key))
        return day, arr.astype(float)  # same precision as a cached day

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        for k, (day, arr) in enumerate(pool.map(get, todo), 1):
            out[day] = arr
            if k % 25 == 0:
                print(f"  downloaded {k}/{len(todo)} days")
    return out


def fill_gaps(arr):
    """Minutes without a trade: price stays at the last close, volume 0. Returns share of real minutes."""
    real = ~np.isnan(arr[:, :, C])
    for i in range(arr.shape[0]):
        if not real[i].any():
            continue
        pos = np.where(real[i], np.arange(arr.shape[1]), 0)
        np.maximum.accumulate(pos, out=pos)
        last_close = arr[i, pos, C]
        first = int(np.argmax(real[i]))
        last_close[:first] = arr[i, first, O]
        miss = ~real[i]
        for k in (O, H, L, C):
            arr[i, miss, k] = last_close[miss]
        arr[i, miss, V] = 0
    return real.mean(axis=1)


def daily_table(symbols, days):
    bars = D.get_bars(symbols, "1Day", days[0][1] - dt.timedelta(days=120), days[-1][2], adjustment="split")
    table = {}
    for s in symbols:
        rows = bars.get(s, [])
        table[s] = ([b["t"].date() for b in rows], np.array([b["c"] for b in rows], float),
                    np.array([b["v"] for b in rows], float))
    return table


def day_stats(table, symbols, day):
    """Same numbers as daytrader.stats_for_day, plus 20- and 50-day averages of the close."""
    out = np.full((len(symbols), 5), np.nan)  # prev_close, avg_vol, sma20, sma50, n_days
    for i, s in enumerate(symbols):
        dates, close, vol = table[s]
        k = bisect.bisect_left(dates, day)  # bars strictly before `day`
        if k >= 10:
            out[i] = (close[k - 1], vol[max(0, k - 20):k].mean(), close[max(0, k - 20):k].mean(),
                      close[max(0, k - 50):k].mean(), k)
    return out


class Day:
    def __init__(self, day, arr, stats):
        self.day = day
        self.real = fill_gaps(arr)
        self.o, self.h, self.l, self.c, self.v = (arr[:, :, k] for k in range(5))
        self.n = arr.shape[1]
        tp = (self.h + self.l + self.c) / 3
        cv = np.cumsum(self.v, axis=1)
        self.cumv = cv
        self.vwap = np.where(cv > 0, np.cumsum(tp * self.v, axis=1) / np.maximum(cv, 1e-12), self.c)
        self.prev_close, self.avg_vol, self.sma20, self.sma50 = (stats[:, k] for k in range(4))
        self.ok = (self.real >= 0.5) & ~np.isnan(self.prev_close) & (self.avg_vol > 0)
        self.spy = arr.shape[0] - 1
        self.ok[self.spy] = False  # SPY is only used as the market filter


def cutoff(F, minute, hold=HOLD):
    """Last minute a trade may start: never so late that its hold would run past the close."""
    return min(minute, F.n - (hold + 5))


# ---------------- rule families ----------------
# Each returns the first signal of every qualifying stock: (minute, tie_rank, stock, stop, target).
# stop/target: ("abs", price) | ("pct", fraction) | ("R", multiple of risk) | ("high", None)
def breakout(F, p):
    """Opening range breakout (the current bot when or=15, stop=mid, tgt=2R, until 11:30, no filters)."""
    n_or, cut = p["or"], cutoff(F, p["until"], p.get("hold", HOLD))
    hi, lo = F.h[:, :n_or].max(1), F.l[:, :n_or].min(1)
    last, vol = F.c[:, n_or - 1], F.v[:, :n_or].sum(1)
    rng = (hi - lo) / last
    ok = F.ok & (vol > 0) & (last > F.vwap[:, n_or - 1]) & (last > F.prev_close) & (rng >= 0.003) & (rng <= 0.03)
    if p["trend"]:
        ok &= F.prev_close > F.sma50
    rvol = np.where(ok, vol / F.avg_vol, -1)
    cands = [s for s in np.argsort(-rvol, kind="stable")[:p.get("top", 3)] if ok[s]]
    mkt = F.c[F.spy, n_or:cut] > F.vwap[F.spy, n_or:cut]
    sigs = []
    for rank, s in enumerate(cands):
        cc = F.c[s, n_or:cut]
        hit = (cc > hi[s]) & (cc <= hi[s] + 0.5 * (hi[s] - lo[s]))
        if p["mkt"]:
            hit &= mkt
        if "floor" in p:  # the bot's market brake: no buys while the S&P 500 is down more than this today
            hit &= F.c[F.spy, n_or:cut] / F.prev_close[F.spy] - 1 > p["floor"]
        if hit.any():
            stop = (hi[s] + lo[s]) / 2 if p["stop"] == "mid" else lo[s]
            sigs.append((n_or + int(np.argmax(hit)), rank, s, ("abs", stop), ("R", p["tgt"])))
    return sigs


def pullback(F, p):
    """Strong stock (up on the day) pulls back to VWAP and closes back above it: buy the bounce."""
    start, cut = 30, cutoff(F, p["until"])
    c, l, vw = F.c[:, start:cut], F.l[:, start:cut], F.vwap[:, start:cut]
    was_above = np.maximum.accumulate(F.c / F.vwap, axis=1)[:, start:cut] >= 1.005
    hit = (c >= F.prev_close[:, None] * (1 + p["up"])) & was_above & (l <= vw * 1.0005) & (c > vw)
    hit &= F.ok[:, None]
    if p["mkt"]:
        hit &= (F.c[F.spy, start:cut] > F.vwap[F.spy, start:cut])[None, :]
    sigs = []
    for s in np.where(hit.any(1))[0]:
        m = start + int(np.argmax(hit[s]))
        tgt = ("high", None) if p["tgt"] == "high" else ("pct", p["tgt"])
        sigs.append((m, -(F.c[s, m] / F.prev_close[s]), s, ("pct", p["stop"]), tgt))
    return sigs


def dip(F, p):
    """Mean reversion: price stretched below VWAP, buy and aim for a bounce back toward VWAP."""
    start, cut = 15, cutoff(F, p["until"])
    c, vw = F.c[:, start:cut], F.vwap[:, start:cut]
    hit = (c <= vw * (1 - p["dip"])) & F.ok[:, None]
    if p["trend"]:
        hit &= (F.prev_close > F.sma50)[:, None]
    if p["mkt"]:
        hit &= (F.c[F.spy, start:cut] / F.prev_close[F.spy] - 1 > -0.01)[None, :]
    sigs = []
    for s in np.where(hit.any(1))[0]:
        m = start + int(np.argmax(hit[s]))
        tgt = ("abs", F.vwap[s, m]) if p["tgt"] == "vwap" else ("pct", p["tgt"])
        sigs.append((m, F.c[s, m] / F.vwap[s, m], s, ("pct", p["stop"]), tgt))
    return sigs


def gap_up_reversal(F, p):
    """Gap down at the open, then the price breaks above its first 5-minute high: buy the recovery."""
    gap = F.o[:, 0] / F.prev_close - 1
    ok = F.ok & (gap <= -p["gap"]) & (gap >= -0.08)
    if p["trend"]:
        ok &= F.prev_close > F.sma20
    hi5, lo5 = F.h[:, :5].max(1), F.l[:, :5].min(1)
    sigs = []
    for s in np.where(ok)[0]:
        hit = F.c[s, 5:60] > hi5[s]
        if hit.any():
            stop = ("abs", lo5[s]) if p["stop"] == "low5" else ("pct", p["stop"])
            tgt = (("abs", F.o[s, 0] + 0.5 * (F.prev_close[s] - F.o[s, 0])) if p["tgt"] == "half_gap"
                   else ("pct", p["tgt"]))
            sigs.append((5 + int(np.argmax(hit)), gap[s], s, stop, tgt))
    return sigs


FAMILIES = {"Breakout": breakout, "VWAP pullback": pullback, "Dip below VWAP": dip,
            "Gap-down recovery": gap_up_reversal}


def grid():
    out = []
    for n_or, stop, tgt, mkt, trend, until in itertools.product(
            (5, 15, 30), ("mid", "low"), (0.5, 1, 1.5, 2), (False, True), (False, True), (120, 240)):
        out.append(("Breakout", dict(**{"or": n_or}, stop=stop, tgt=tgt, mkt=mkt, trend=trend, until=until)))
    for up, stop, tgt, mkt, until in itertools.product(
            (0.005, 0.01, 0.02), (0.005, 0.0075, 0.01, 0.015), (0.003, 0.005, 0.0075, 0.01, "high"),
            (False, True), (120, 240)):
        out.append(("VWAP pullback", dict(up=up, stop=stop, tgt=tgt, mkt=mkt, until=until)))
    for dip_, tgt, stop, mkt, trend, until in itertools.product(
            (0.005, 0.0075, 0.01, 0.015, 0.02), ("vwap", 0.003, 0.005, 0.0075, 0.01),
            (0.01, 0.015, 0.02, 0.03), (False, True), (False, True), (120, 240)):
        out.append(("Dip below VWAP", dict(dip=dip_, tgt=tgt, stop=stop, mkt=mkt, trend=trend, until=until)))
    for gap, stop, tgt, trend in itertools.product(
            (0.01, 0.02, 0.03), ("low5", 0.01, 0.015), ("half_gap", 0.005, 0.01), (False, True)):
        out.append(("Gap-down recovery", dict(gap=gap, stop=stop, tgt=tgt, trend=trend)))
    return out


CURRENT = ("Breakout", {"or": 15, "stop": "mid", "tgt": 2, "mkt": False, "trend": False, "until": 120})


# ---------------- simulation (same entry, exit, sizing and costs as the bot's backtest) ----------------
def simulate_signal(F, m, s, stop_spec, tgt_spec, fee=FEE, slip=SLIP, cap=D.MAX_POSITION):
    """Buy stock s at the open after signal minute m; None if the plan isn't valid."""
    k0 = m + 1
    if k0 >= F.n - 5:
        return None
    entry = F.o[s, k0] * (1 + slip)
    stop = stop_spec[1] if stop_spec[0] == "abs" else entry * (1 - stop_spec[1])
    risk = (entry - stop) / entry
    if risk < 0.001 or risk > MAX_STOP + 1e-9:
        return None
    kind, val = tgt_spec
    target = (entry + val * (entry - stop) if kind == "R" else entry * (1 + val) if kind == "pct"
              else F.h[s, :m + 1].max() if kind == "high" else val)
    if target <= entry * 1.001:
        return None
    deadline = min(k0 + HOLD, F.n - 5)
    lo, hi, op = F.l[s, k0:deadline], F.h[s, k0:deadline], F.o[s, k0:deadline]
    hs, ht = lo <= stop, hi >= target
    i_s = int(np.argmax(hs)) if hs.any() else 10 ** 6
    i_t = int(np.argmax(ht)) if ht.any() else 10 ** 6
    if i_s == i_t == 10 ** 6:
        k_exit, raw, why = deadline, F.o[s, deadline], "time"
    elif i_s <= i_t:  # stop first when both happen in the same minute (cautious)
        k_exit, raw, why = k0 + i_s, min(stop, op[i_s]), "stop"
    else:
        k_exit, raw, why = k0 + i_t, max(target, op[i_t]), "target"
    exit_ = raw * (1 - slip)
    size = min(cap, RISK / risk)
    ret = size * ((exit_ / entry - 1) - fee - fee * exit_ / entry)
    best = F.h[s, k0:k_exit + 1].max() / entry - 1  # best price reached while holding
    return {"day": F.day, "stock": s, "minute": m, "entry": entry, "exit": exit_, "why": why,
            "ret": ret, "gross": size * (raw / F.o[s, k0] - 1), "best": best, "stop": stop, "target": target,
            "mkt_up": F.c[F.spy, m] > F.vwap[F.spy, m], "k_exit": k_exit, "size": size}


def trade_for_day(F, sigs, fee=FEE, slip=SLIP):
    for m, _, s, stop_spec, tgt_spec in sorted(sigs, key=lambda x: (x[0], x[1])):
        t = simulate_signal(F, m, s, stop_spec, tgt_spec, fee, slip)
        if t:
            return t
    return None


def run(days, family, params, fee=FEE, slip=SLIP):
    fn = FAMILIES[family]
    return [t for t in (trade_for_day(F, fn(F, params), fee, slip) for F in days) if t]


def stats(trades):
    if not trades:
        return {"n": 0, "wr": 0.0, "total": 0.0, "avg": 0.0, "dd": 0.0}
    r = np.array([t["ret"] for t in trades])
    eq = np.cumprod(1 + r)
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    return {"n": len(r), "wr": float((r > 0).mean()), "total": float(eq[-1] - 1), "avg": float(r.mean()),
            "dd": float((1 - eq / peak).max())}


def describe(family, p):
    def pct(x):
        return f"{x * 100:g}%"
    until = {120: "until 11:30", 240: "until 13:30"}.get(p.get("until"), "")
    if family == "Breakout":
        s = (f"{p['or']}-min opening range, stop at range {'middle' if p['stop'] == 'mid' else 'bottom'}, "
             f"target {p['tgt']:g}x risk, {until}")
    elif family == "VWAP pullback":
        s = (f"stock up {pct(p['up'])}+ on the day, stop -{pct(p['stop'])}, "
             f"target {'day high' if p['tgt'] == 'high' else '+' + pct(p['tgt'])}, {until}")
    elif family == "Dip below VWAP":
        s = (f"{pct(p['dip'])} below VWAP, target {'back to VWAP' if p['tgt'] == 'vwap' else '+' + pct(p['tgt'])}, "
             f"stop -{pct(p['stop'])}, {until}")
    else:
        s = (f"gap down {pct(p['gap'])}+, stop {'5-min low' if p['stop'] == 'low5' else '-' + pct(p['stop'])}, "
             f"target {'half the gap' if p['tgt'] == 'half_gap' else '+' + pct(p['tgt'])}")
    if p.get("mkt"):
        s += ", only when market is OK"
    if p.get("trend"):
        s += ", only stocks in an uptrend"
    return f"{family}: {s}"


def line(st):
    return (f"{st['n']:3d} trades | {st['wr'] * 100:3.0f}% winners | {st['total'] * 100:+6.1f}% | "
            f"avg {st['avg'] * 100:+.2f}%/trade | worst drop {st['dd'] * 100:4.1f}%")


# ---------------- report ----------------
def gap_analysis(trades):
    if not trades:
        return ["(no trades)"]
    st = stats(trades)
    r = np.array([t["ret"] for t in trades])
    gross = np.array([t["gross"] for t in trades])
    losers = [t for t in trades if t["ret"] <= 0]
    why = {k: sum(t["why"] == k for t in trades) for k in ("target", "stop", "time")}
    nearly = sum(t["best"] >= 0.003 for t in losers)
    out = [line(st),
           f"Exits: {why['target']} reached the target, {why['stop']} hit the stop, {why['time']} closed at 2 hours.",
           f"Average winner {r[r > 0].mean() * 100 if (r > 0).any() else 0:+.2f}%, "
           f"average loser {r[r <= 0].mean() * 100:+.2f}% of the account.",
           f"Same trades with no fees and no slippage: {(np.prod(1 + gross) - 1) * 100:+.1f}% "
           f"({(gross > 0).mean() * 100:.0f}% winners).",
           f"Losing trades that were at least +0.3% up at some point: {nearly} of {len(losers)}."]
    for label, part in (("Market above its VWAP at entry", [t for t in trades if t["mkt_up"]]),
                        ("Market below its VWAP at entry", [t for t in trades if not t["mkt_up"]])):
        s2 = stats(part)
        out.append(f"{label}: {s2['n']} trades, {s2['wr'] * 100:.0f}% winners, {s2['total'] * 100:+.1f}%")
    for a, b in ((15, 45), (45, 90), (90, 240)):
        part = [t for t in trades if a <= t["minute"] < b]
        s2 = stats(part)
        t0 = (dt.datetime(2000, 1, 1, 9, 30) + dt.timedelta(minutes=a)).strftime("%H:%M")
        t1 = (dt.datetime(2000, 1, 1, 9, 30) + dt.timedelta(minutes=b)).strftime("%H:%M")
        out.append(f"Entries {t0}-{t1} New York: {s2['n']} trades, {s2['wr'] * 100:.0f}% winners, "
                   f"{s2['total'] * 100:+.1f}%")
    return out


def load_study():
    """A year of prices, split into tuning months (older 70%) and test months (newest 30%)."""
    symbols = syms()
    today = D.now_ny().date()
    days = D.calendar(today - dt.timedelta(days=int(DAYS * 1.6) + 10), today - dt.timedelta(days=1))[-DAYS:]
    raw = load_days(days, symbols)
    table = daily_table(symbols, days)
    feats = [Day(day, raw[day], day_stats(table, symbols, day)) for day, _, _ in days]
    split = int(len(feats) * (1 - TEST_SHARE))
    return symbols, today, feats, feats[:split], feats[split:]


def research():
    symbols, today, feats, tune, test = load_study()
    names = symbols

    results = []
    for family, p in grid():
        a, b = run(tune, family, p), run(test, family, p)
        results.append((family, p, stats(a), stats(b), a + b))
    print(f"Tested {len(results)} variations.")

    cur = next(x for x in results if (x[0], x[1]) == CURRENT)
    lines = [f"🔬 Research: {len(results)} rule variations, {len(feats)} trading days "
             f"({feats[0].day} to {feats[-1].day})",
             f"Tuning months: {tune[0].day} to {tune[-1].day} ({len(tune)} days). "
             f"Test months (never used to choose): {test[0].day} to {test[-1].day} ({len(test)} days).",
             f"Costs: Sahm {FEE * 100:.3f}% per order + {SLIP * 100:.2f}% slippage each way. "
             "Max 2-hour hold, one trade per day, medium risk sizing.", "",
             "== Where the current rules lose (whole year) =="]
    lines += gap_analysis(cur[4])
    lines += ["", f"Current rules, tuning months: {line(cur[2])}", f"Current rules, test months:   {line(cur[3])}"]

    eligible = [x for x in results if x[2]["n"] >= MIN_TRADES]
    goal = sorted([x for x in eligible if x[2]["wr"] >= GOAL_WIN_RATE], key=lambda x: -x[2]["total"])
    best = sorted(eligible, key=lambda x: -x[2]["total"])
    lines += ["", f"== Rules with {GOAL_WIN_RATE * 100:.0f}%+ winners in the tuning months "
                  f"(at least {MIN_TRADES} trades): {len(goal)} found =="]
    for family, p, a, b, _ in goal[:12]:
        lines += [describe(family, p), f"   tuning: {line(a)}", f"   TEST:   {line(b)}"]
    if goal:
        pos = [x for x in goal if x[2]["total"] > 0]
        held = [x for x in pos if x[3]["wr"] >= GOAL_WIN_RATE and x[3]["total"] > 0]
        lines.append(f"Of {len(pos)} with 80%+ winners AND profit in the tuning months, "
                     f"{len(held)} kept both in the test months.")
    lines += ["", "== Most profitable rules in the tuning months (any win rate) =="]
    for family, p, a, b, _ in best[:12]:
        lines += [describe(family, p), f"   tuning: {line(a)}", f"   TEST:   {line(b)}"]
    pos_test = sum(x[3]["total"] > 0 for x in eligible)
    lines.append(f"Of {len(eligible)} rules with enough trades, {pos_test} made money in the test months.")

    if best:
        family, p, a, b, trades = best[0]
        lines += ["", "== Trades of the top rule in the test months ==", describe(family, p)]
        for t in [t for t in trades if t["day"] >= test[0].day]:
            lines.append(f"{t['day']} {names[t['stock']]:<5} {t['why']:<6} {t['ret'] * 100:+.2f}%")

    # Would cheaper trading rescue any rule? Same test with no commission and tiny slippage.
    low = []
    for family, p in grid():
        a, b = run(tune, family, p, 0.0, LOW_SLIP), run(test, family, p, 0.0, LOW_SLIP)
        low.append((family, p, stats(a), stats(b)))
    low_ok = [x for x in low if x[2]["n"] >= MIN_TRADES]
    lines += ["", f"== What if trading cost nothing (no commission, {LOW_SLIP * 100:.2f}% slippage)? =="]
    cur_low = next(x for x in low if (x[0], x[1]) == CURRENT)
    lines += [f"Current rules, tuning months: {line(cur_low[2])}", f"Current rules, test months:   {line(cur_low[3])}"]
    goal_low = [x for x in low_ok if x[2]["wr"] >= GOAL_WIN_RATE and x[2]["total"] > 0]
    lines.append(f"Rules with 80%+ winners AND profit in the tuning months: {len(goal_low)}; "
                 f"still 80%+ and profitable in the test months: "
                 f"{sum(x[3]['wr'] >= GOAL_WIN_RATE and x[3]['total'] > 0 for x in goal_low)}.")
    for family, p, a, b in sorted(low_ok, key=lambda x: -x[2]["total"])[:6]:
        lines += [describe(family, p), f"   tuning: {line(a)}", f"   TEST:   {line(b)}"]
    lines.append(f"Of {len(low_ok)} rules with enough trades, {sum(x[3]['total'] > 0 for x in low_ok)} "
                 "made money in the test months.")

    path = D.write_report(f"research-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"🔬 Research finished: {len(results)} rule variations tested on {len(feats)} trading days. "
           f"Full report saved in the repo ({path}).")


# ---------------- AI filter: at most 2 trades a day, only the highest-confidence setups ----------------
def _bo(n_or, stop):
    return ("Breakout", {"or": n_or, "stop": stop, "tgt": 1, "mkt": False, "trend": False, "until": 240, "top": 60})


def _pb(up, stop, tgt):
    return ("VWAP pullback", {"up": up, "stop": stop, "tgt": tgt, "mkt": False, "until": 240})


def _dip(d, tgt, stop):
    return ("Dip below VWAP", {"dip": d, "tgt": tgt, "stop": stop, "mkt": False, "trend": False, "until": 240})


def _gap(g):
    return ("Gap-down recovery", {"gap": g, "stop": 0.01, "tgt": 0.01, "trend": False})


# Every setup's target is at least as far as its stop, so a high win rate really means profit.
GENERATORS = [_bo(5, "mid"), _bo(5, "low"), _bo(15, "mid"), _bo(15, "low"), _bo(30, "low"),
              _pb(0.005, 0.0075, 0.0075), _pb(0.005, 0.01, 0.01), _pb(0.01, 0.0075, 0.01),
              _dip(0.0075, 0.0075, 0.0075), _dip(0.01, 0.01, 0.01), _dip(0.015, 0.01, 0.01),
              _dip(0.02, 0.015, 0.015), _gap(0.01), _gap(0.02)]
HALF = 0.5           # each of the 2 daily trades uses half the account
MAX_PER_DAY = 2
QUANTILES = (0.0, 0.5, 0.75, 0.9, 0.95, 0.98, 0.99, 0.995)
MIN_PICKS = 25       # fewest picks a confidence bar needs in the check period


def setup_features(F, s, m, g, t):
    """What the AI sees: only information available when the signal fires."""
    c, a, k = F.c[s], max(m - 15, 0), min(15, m + 1)
    rets = np.diff(np.log(c[a:m + 1]))
    return [g, m, c[m] / F.prev_close[s] - 1, c[m] / F.o[s, 0] - 1, F.o[s, 0] / F.prev_close[s] - 1,
            c[m] / F.vwap[s, m] - 1, (F.h[s, :k].max() - F.l[s, :k].min()) / F.o[s, 0],
            F.cumv[s, m] / (F.avg_vol[s] * (m + 1) / F.n), c[m] / c[a] - 1,
            float(rets.std()) if len(rets) > 1 else 0.0, c[m] / F.h[s, :m + 1].max() - 1,
            c[m] / F.l[s, :m + 1].min() - 1, F.prev_close[s] / F.sma20[s] - 1, F.prev_close[s] / F.sma50[s] - 1,
            F.c[F.spy, m] / F.prev_close[F.spy] - 1, F.c[F.spy, m] / F.vwap[F.spy, m] - 1,
            1 - t["stop"] / t["entry"], t["target"] / t["entry"] - 1]


def collect(days):
    """Every setup on every day, with what the AI would see and how the trade turned out."""
    rows = []
    for F in days:
        for g, (family, p) in enumerate(GENERATORS):
            for m, _, s, stop_spec, tgt_spec in FAMILIES[family](F, p):
                t = simulate_signal(F, m, s, stop_spec, tgt_spec, cap=HALF)
                if t:
                    rows.append({"day": F.day, "minute": m, "stock": s, "x": setup_features(F, s, m, g, t),
                                 "ret": t["ret"], "why": t["why"], "setup": g})
    return rows


def pick(rows, scores, bar):
    """Live-style choice: go through the day in time order, take a setup if it clears the bar,
    at most 2 a day, never the same stock twice."""
    by_day = {}
    for i, r in enumerate(rows):
        by_day.setdefault(r["day"], []).append(i)
    taken = []
    for day in sorted(by_day):
        stocks = set()
        for i in sorted(by_day[day], key=lambda i: (rows[i]["minute"], -scores[i])):
            if len(stocks) >= MAX_PER_DAY:
                break
            if scores[i] >= bar and rows[i]["stock"] not in stocks:
                taken.append(i)
                stocks.add(rows[i]["stock"])
    return taken


def portfolio(rows, idx):
    """Trades share a day's account (half each), so the account grows day by day."""
    if not idx:
        return {"n": 0, "wr": 0.0, "total": 0.0, "avg": 0.0, "dd": 0.0}
    r = np.array([rows[i]["ret"] for i in idx])
    daily = {}
    for i in idx:
        daily[rows[i]["day"]] = daily.get(rows[i]["day"], 0.0) + rows[i]["ret"]
    eq = np.cumprod(1 + np.array([daily[d] for d in sorted(daily)]))
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    return {"n": len(r), "wr": float((r > 0).mean()), "total": float(eq[-1] - 1), "avg": float(r.mean()),
            "dd": float((1 - eq / peak).max())}


def fit(rows):
    from sklearn.ensemble import HistGradientBoostingClassifier
    model = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=200,
                                           l2_regularization=1.0, categorical_features=[0], random_state=0)
    model.fit(np.array([r["x"] for r in rows]), np.array([r["ret"] > 0 for r in rows]))
    return model


def score(model, rows):
    return model.predict_proba(np.array([r["x"] for r in rows]))[:, 1] if rows else np.array([])


def ai_research():
    symbols, today, feats, tune, test = load_study()
    cut = int(len(tune) * 0.7)
    learn, check = tune[:cut], tune[cut:]
    rows_learn, rows_check, rows_test = collect(learn), collect(check), collect(test)
    print(f"Setups: {len(rows_learn)} learn, {len(rows_check)} check, {len(rows_test)} test")

    # 1. Choose the confidence bar using only the tuning months (learn on the older part, check on the rest).
    model = fit(rows_learn)
    s_learn, s_check = score(model, rows_learn), score(model, rows_check)
    table, options = [], []
    for q in QUANTILES:
        bar = float(np.quantile(s_learn, q))
        st = portfolio(rows_check, pick(rows_check, s_check, bar))
        table.append(f"  best {100 - q * 100:g}% of setups: {line(st)}")
        options.append((q, st))
    ok = [(q, st) for q, st in options if st["n"] >= MIN_PICKS]
    goal = [x for x in ok if x[1]["wr"] >= GOAL_WIN_RATE and x[1]["total"] > 0]
    profit = [x for x in ok if x[1]["total"] > 0]
    q_best = (max(goal, key=lambda x: x[1]["total"]) if goal else max(profit, key=lambda x: x[1]["wr"]) if profit
              else max(ok, key=lambda x: x[1]["total"]) if ok else (0.0, None))[0]

    # 2. Retrain on all tuning months, then judge once on the test months.
    rows_tune = rows_learn + rows_check
    final = fit(rows_tune)
    bar = float(np.quantile(score(final, rows_tune), q_best))
    s_test = score(final, rows_test)
    picks = pick(rows_test, s_test, bar)
    ai = portfolio(rows_test, picks)
    plain = portfolio(rows_test, pick(rows_test, np.ones(len(rows_test)), 0.0))
    expected = float(np.mean([s_test[i] for i in picks])) if picks else 0.0

    def win_share(rows):
        return np.mean([r["ret"] > 0 for r in rows]) * 100 if rows else 0.0

    lines = [f"🤖 AI filter test: at most {MAX_PER_DAY} trades a day (half the account each), "
             "only the setups the AI is most confident about.",
             f"Data: {len(feats)} trading days ({feats[0].day} to {feats[-1].day}), {len(symbols) - 1} stocks, "
             f"Sahm {FEE * 100:.3f}% per order + {SLIP * 100:.2f}% slippage each way, max 2-hour hold.",
             f"Setups: {len(rows_learn) + len(rows_check) + len(rows_test)} from {len(GENERATORS)} setup types "
             "(breakouts, VWAP pullbacks, dips below VWAP, gap-down recoveries); every target is at least as "
             "far as its stop.",
             f"Share of all setups that won: tuning months {win_share(rows_tune):.0f}%, "
             f"test months {win_share(rows_test):.0f}%.", "",
             f"== Choosing the confidence bar (AI learned {learn[0].day} to {learn[-1].day}, "
             f"checked {check[0].day} to {check[-1].day}) =="] + table
    lines.append(f"Chosen bar: the best {100 - q_best * 100:g}% of setups.")
    lines += ["", f"== Result on the test months {test[0].day} to {test[-1].day} (never seen) ==",
              f"No filter (first 2 setups each day): {line(plain)}",
              f"AI filter:                           {line(ai)}",
              f"The AI expected {expected * 100:.0f}% of its picks to win; {ai['wr'] * 100:.0f}% actually won."]
    verdict = ("MET: 80%+ winners and a profit on months it never saw."
               if ai["n"] >= MIN_PICKS and ai["wr"] >= GOAL_WIN_RATE and ai["total"] > 0 else
               "NOT MET: the AI's picks did not reach 80% winners with a profit on the months it never saw.")
    lines.append(f"Goal (80%+ winners and profit): {verdict}")
    if picks:
        lines += ["", "== AI picks in the test months =="]
        for i in picks:
            r = rows_test[i]
            t0 = (dt.datetime(2000, 1, 1, 9, 30) + dt.timedelta(minutes=r["minute"] + 1)).strftime("%H:%M")
            lines.append(f"{r['day']} {t0} {symbols[r['stock']]:<5} {describe(*GENERATORS[r['setup']]).split(':')[0]:<17} "
                         f"{r['why']:<6} {r['ret'] * 100:+.2f}% (AI confidence {s_test[i] * 100:.0f}%)")

    path = D.write_report(f"research-ai-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"🤖 AI filter test finished. Full report saved in the repo ({path}).")


# ---------------- market and news check for the v2 rules, over the whole year ----------------
def had_news(symbol, when):
    """True/False: headlines about the stock in the 24 hours before `when` (None if unknown)."""
    try:
        r = D.api("GET", D.DATA_URL + "/v1beta1/news", {
            "symbols": symbol, "start": D.iso(when - dt.timedelta(hours=24)), "end": D.iso(when), "limit": 1})
        return bool(r.get("news"))
    except Exception as e:
        print(f"news unavailable for {symbol}: {e}")
        return None


def market_research():
    """Every first breakout of the 5 busiest morning stocks (v2 rules: entries until 1:55 PM, a third of the
    money each), split by what the S&P 500 was doing and by news, in the tuning and the test months."""
    symbols, today, feats, tune, test = load_study()
    p = {"or": 15, "stop": "mid", "tgt": 2, "mkt": False, "trend": False, "until": 265, "top": 5}
    rows = []
    for part, days in (("tuning", tune), ("test", test)):
        for F in days:
            for m, _, s, stop_spec, tgt_spec in breakout(F, p):
                t = simulate_signal(F, m, s, stop_spec, tgt_spec, cap=1 / 3)
                if t:
                    when = dt.datetime.combine(F.day, dt.time(9, 30), D.NY) + dt.timedelta(minutes=m + 1)
                    rows.append({"part": part, "ret": t["ret"], "news": had_news(symbols[s], when),
                                 "spy": F.c[F.spy, m] / F.prev_close[F.spy] - 1,
                                 "spy_vwap": F.c[F.spy, m] / F.vwap[F.spy, m] - 1})
    groups = [("S&P 500 up on the day", lambda r: r["spy"] > 0),
              ("S&P 500 flat or down", lambda r: r["spy"] <= 0),
              ("S&P 500 down more than 1%", lambda r: r["spy"] <= -0.01),
              ("S&P 500 above its VWAP", lambda r: r["spy_vwap"] > 0),
              ("S&P 500 below its VWAP", lambda r: r["spy_vwap"] <= 0),
              ("Up on the day AND above VWAP", lambda r: r["spy"] > 0 and r["spy_vwap"] > 0),
              ("News in the 24 hours before", lambda r: r["news"] is True),
              ("No news", lambda r: r["news"] is False),
              ("Market up AND news", lambda r: r["spy"] > 0 and r["news"] is True),
              ("Everything", lambda r: True)]
    lines = [f"📊 Market & news check: every first breakout of the 5 busiest morning stocks, {len(feats)} days "
             f"({feats[0].day} to {feats[-1].day}), Sahm costs, max 2-hour hold.",
             f"Tuning months {tune[0].day} to {tune[-1].day}; test months {test[0].day} to {test[-1].day}.", ""]
    for label, keep in groups:
        cells = []
        for part in ("tuning", "test"):
            g = [r for r in rows if r["part"] == part and keep(r)]
            if g:
                r_ = np.array([x["ret"] for x in g])
                cells.append(f"{part}: {len(g)} trades, {(r_ > 0).mean() * 100:.0f}% winners, "
                             f"avg {r_.mean() * 100:+.2f}% of the account")
            else:
                cells.append(f"{part}: no trades")
        lines.append(f"{label}: " + " | ".join(cells))
    path = D.write_report(f"research-market-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")


# ---------------- would the losing trades have won if held longer? ----------------
V2 = {"or": 15, "stop": "mid", "tgt": 2, "mkt": False, "trend": False, "until": 265, "top": 5, "floor": -0.01}
HOLDS = [("2h", "No stop, 2 hours"), ("close", "No stop, until the day's close"),
         (1, "No stop, up to 1 more day"), (3, "No stop, up to 3 more days"), (5, "No stop, up to 5 more days")]


def clock12(t):
    return t.strftime("%I:%M %p").lstrip("0")


def v2_day(F):
    """The bot's current rules on one day (daytrader v2): each of the 5 busiest morning stocks' first
    breakout while the S&P 500 isn't down more than 1%, earliest first, at most 3, a third of the money each."""
    out = []
    for m, _, s, stop_spec, tgt_spec in sorted(breakout(F, V2), key=lambda x: (x[0], x[1])):
        if len(out) >= 3:
            break
        t = simulate_signal(F, m, s, stop_spec, tgt_spec, cap=1 / 3)
        if t:
            out.append(t)
    return out


def net(entry, raw_exit):
    """Result after slippage and Sahm fees, as a fraction of the money put into the trade."""
    x = raw_exit * (1 - SLIP)
    return (x / entry - 1) - FEE - FEE * x / entry


def break_even(entry):
    """Price at which selling gives back exactly what went in, after slippage and fees."""
    return entry * (1 + FEE) / ((1 - SLIP) * (1 - FEE))


def daily_ohlc(symbols, feats):
    start = dt.datetime.combine(feats[0].day, dt.time(9, 30), D.NY) - dt.timedelta(days=5)
    end = dt.datetime.combine(feats[-1].day, dt.time(16, 0), D.NY)
    bars = D.get_bars(symbols, "1Day", start, end, adjustment="split")
    return {s: {b["t"].date(): (b["o"], b["h"], b["l"], b["c"]) for b in bars.get(s, [])} for s in symbols}


def hold_outcomes(feats, i, t, ohlc, name):
    """For one trade: its result under each 'hold longer, no stop' rule (None if the data runs out),
    and, after the bot actually sold, whether the price came back above break-even."""
    F = feats[i]
    s, k0, entry, target = t["stock"], t["minute"] + 1, t["entry"], t["target"]
    rest = F.h[s, k0:F.n] >= target
    first_hit = k0 + int(np.argmax(rest)) if rest.any() else None
    res = {}
    for key, _ in HOLDS:
        if key in ("2h", "close"):
            end = min(k0 + HOLD, F.n - 5) if key == "2h" else F.n - 5
            res[key] = net(entry, max(target, F.o[s, first_hit]) if first_hit is not None and first_hit < end
                           else F.o[s, end])
            continue
        if first_hit is not None:
            res[key] = net(entry, max(target, F.o[s, first_hit]))
            continue
        if i + key >= len(feats):
            res[key] = None
            continue
        raw, last = None, None
        for d in range(1, key + 1):
            bar = ohlc[name].get(feats[i + d].day)
            if bar is None:
                continue
            o, h, _, c = bar
            if h >= target:
                raw = max(target, o)
                break
            last = c
        res[key] = net(entry, raw if raw is not None else last) if (raw or last) else None

    be = break_even(entry)
    kx = t["k_exit"]
    end2h = min(k0 + HOLD, F.n - 5)
    back = {"2h": bool((F.h[s, kx + 1:end2h] >= be).any()) if kx + 1 < end2h else None,
            "close": bool((F.h[s, kx + 1:F.n] >= be).any())}
    for d in (1, 3, 5):
        if back["close"]:
            back[d] = True
        elif i + d >= len(feats):
            back[d] = None
        else:
            back[d] = any((ohlc[name].get(feats[i + j].day) or (0, 0, 0, 0))[1] >= be for j in range(1, d + 1))
    return res, back


def fetch_live_trades():
    """The bot's live paper trades so far (journal/trades.csv on main)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "Mr-MTB/stock-assistant")
    headers = {"Accept": "application/vnd.github.raw"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    url = f"https://api.github.com/repos/{repo}/contents/journal/trades.csv?ref=main"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode())))


def live_after_sale(rows):
    """For each live trade: what the price did after the bot sold, until the close (or now)."""
    lines, now = [], D.now_ny()
    for r in rows:
        day = dt.date.fromisoformat(r["date"])
        cal = D.calendar(day, day)
        if not cal:
            continue
        close = cal[0][2]
        sold = dt.datetime.combine(day, dt.datetime.strptime(r["exit_time_ny"], "%I:%M %p").time(), D.NY)
        end = min(close, now)
        entry, exit_, target = float(r["entry"]), float(r["exit"]), float(r["target"])
        bars = [b for b in D.get_bars([r["symbol"]], "1Min", sold, end).get(r["symbol"], []) if b["t"] >= sold]
        head = (f"{r['symbol']} ({r['date']}): bought ${entry:.2f}, sold {r['exit_time_ny']} at ${exit_:.2f} "
                f"({r['exit_reason']}), {float(r['pnl_pct']):+.2f}% → {r['result'].upper()}")
        if not bars:
            lines += [head, "   (no prices after the sale yet)"]
            continue
        be = break_even(entry)
        hi = max(bars, key=lambda b: b["h"])
        back = next((b for b in bars if b["h"] >= be), None)
        tgt = next((b for b in bars if b["h"] >= target), None)
        last = bars[-1]["c"]
        held = net(entry, max(target, tgt["o"]) if tgt else last)
        until = "the close (4:00 PM)" if now >= close else f"now ({clock12(now)} New York, market still open)"
        lines += [head,
                  f"   After the sale: highest ${hi['h']:.2f} at {clock12(hi['t'])}; price at {until}: ${last:.2f}",
                  f"   Back above break-even (${be:.2f})? " + (f"yes, at {clock12(back['t'])}" if back else "no"),
                  f"   Reached the target (${target:.2f})? " + (f"yes, at {clock12(tgt['t'])}" if tgt else "no"),
                  f"   Holding with no stop until {until.split(' (')[0]}: {held * 100:+.2f}% "
                  f"(instead of {float(r['pnl_pct']):+.2f}%)"]
    return lines or ["(no live trades yet)"]


def hold_research(live_rows=None):
    symbols, today, feats, tune, test = load_study()
    ohlc = daily_ohlc(symbols, feats)
    split_day = test[0].day
    rows = []
    for i, F in enumerate(feats):
        for t in v2_day(F):
            res, back = hold_outcomes(feats, i, t, ohlc, symbols[t["stock"]])
            rows.append({"day": F.day, "part": "test" if F.day >= split_day else "tuning", "t": t,
                         "now": t["ret"] / t["size"], "res": res, "back": back})

    lines = [f"⏳ Would the losing trades have won if held longer? The bot's current rules on "
             f"{len(feats)} trading days ({feats[0].day} to {feats[-1].day}), Sahm costs included.", ""]
    lines += ["== Today's live paper trades: what happened after the bot sold =="]
    try:
        lines += live_after_sale(live_rows if live_rows is not None else fetch_live_trades())
    except Exception as e:
        lines.append(f"(Couldn't check the live trades: {e})")

    losers = [x for x in rows if x["now"] <= 0]
    stops = [x for x in losers if x["t"]["why"] == "stop"]
    lines += ["", f"== The past year: {len(rows)} trades, {len(rows) - len(losers)} winners, "
                  f"{len(losers)} losers ({len(stops)} hit the stop, {len(losers) - len(stops)} closed at 2 hours) =="]

    def share(group, key):
        known = [x for x in group if x["back"][key] is not None]
        k = sum(x["back"][key] for x in known)
        return f"{k} of {len(known)} ({k / len(known) * 100:.0f}%)" if known else "no data"

    lines += [f"Stop-outs that came back above break-even before the 2 hours were up: {share(stops, '2h')}",
              f"Losers that came back above break-even later the same day: {share(losers, 'close')}",
              f"... within 1 more day: {share(losers, 1)} | 3 more days: {share(losers, 3)} | "
              f"5 more days: {share(losers, 5)}",
              "('Came back' means the price touched that level at some moment; you'd have had to sell exactly then.)"]
    lines += ["", "If the bot had kept its losing trades instead of selling (no stop, same target):"]
    for key, label in HOLDS:
        known = [x for x in losers if x["res"][key] is not None]
        if known:
            won = sum(x["res"][key] > 0 for x in known)
            avg = np.mean([x["res"][key] for x in known]) * 100
            lines.append(f"  {label}: {won} of {len(known)} losers would have ended as wins ({won / len(known) * 100:.0f}%); "
                         f"the losers' average would be {avg:+.2f}% instead of "
                         f"{np.mean([x['now'] for x in known]) * 100:+.2f}%")

    full = [x for x in rows if all(x["res"][k] is not None for k, _ in HOLDS)]
    lines += ["", f"== Fair test: the same rule for EVERY trade (winners too), {len(full)} trades with 5 days of data after them ==",
              "(per trade: % of the money in that trade; total: % of the whole account, added up)"]
    for part in ("tuning", "test"):
        group = [x for x in full if x["part"] == part]
        if not group:
            continue
        a, b = group[0]["day"], group[-1]["day"]
        lines.append(f"{'Older' if part == 'tuning' else 'Newer'} months ({a} to {b}, {len(group)} trades):")
        for key, label in [(None, "Bot's rules now (stop, target, 2 hours)")] + HOLDS:
            r = np.array([x["now"] if key is None else x["res"][key] for x in group])
            acct = np.array([x["t"]["size"] for x in group]) * r
            lines.append(f"  {label:<40} {(r > 0).mean() * 100:3.0f}% winners | avg {r.mean() * 100:+.2f}% | "
                         f"worst {r.min() * 100:+.1f}% | total {acct.sum() * 100:+.1f}% of the account")
    window = [x for x in rows if dt.date(2026, 7, 9) <= x["day"] <= dt.date(2026, 10, 1)]
    if window:
        lines += ["", f"Check: same rules, Jul 9 to Oct 1: {len(window)} trades, "
                      f"{sum(x['now'] > 0 for x in window) / len(window) * 100:.0f}% winners "
                      "(the bot's own backtest of those days: 172 trades, 30%)."]

    path = D.write_report(f"research-hold-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"⏳ Hold-longer study finished. Full report saved in the repo ({path}).")


# ---------------- hold until (just before) the close: which stop, target, start and times work best? ----------------
EXIT_RULES = [  # (name, stop at, target in R (None = ride to the end), move stop to break-even at +R, trail R)
    ("stop mid, target 2R", "mid", 2, None, None),
    ("stop mid, target 3R", "mid", 3, None, None),
    ("stop mid, no target", "mid", None, None, None),
    ("wide stop (range low), target 2R", "low", 2, None, None),
    ("wide stop (range low), no target", "low", None, None, None),
    ("stop mid, target 3R, break-even at +1R", "mid", 3, 1, None),
    ("stop mid, trailing 1R after +1R", "mid", None, None, 1),
    ("no stop, target 2R", None, 2, None, None),
]
NOW_RULE = ("stop mid, target 2R, 2 hours", "mid", 2, None, None)
LAST_ENTRIES = {"1:55 PM": 265, "2:50 PM": 320}
EXIT_TIMES = {"3:20 PM": 40, "3:55 PM": 5}  # minutes before the close


def walk_exit(F, s, k0, entry, stop, target, end, be_at=None, trail=None):
    """Minute by minute from k0 until `end`; returns (raw exit price, reason). Same minute: stop before target."""
    risk = entry - stop if stop is not None else None
    cur, best = stop, entry
    lo, hi, op = F.l[s], F.h[s], F.o[s]
    for k in range(k0, end):
        if cur is not None and lo[k] <= cur:
            return min(cur, op[k]), "stop" if cur == stop else "protect"
        if target is not None and hi[k] >= target:
            return max(target, op[k]), "target"
        best = max(best, hi[k])
        if risk:
            if be_at and best >= entry + be_at * risk:
                cur = max(cur, break_even(entry))
            if trail and best >= entry + risk:
                cur = max(cur, best - trail * risk)
    return op[end], "end"


def exits_day(F, n_or, until, rule, before_close):
    """One day: the bot's v2 entries (first breakouts of the 5 busiest morning stocks while the S&P 500 isn't
    down more than 1%, earliest first, at most 3, a third of the money each) with this rule's exits.
    before_close None = the bot today (2-hour hold); otherwise exit everything that many minutes before the close."""
    _, stop_kind, tgt_r, be_at, trail = rule
    hold = HOLD if before_close is None else before_close + 25  # last entry at least 30 min before the exit
    p = dict(V2, **{"or": n_or, "until": until, "stop": stop_kind or "mid", "hold": hold})
    out = []
    for m, _, s, (_, stop), _ in sorted(breakout(F, p), key=lambda x: (x[0], x[1])):
        if len(out) >= 3:
            break
        k0 = m + 1
        if k0 >= F.n - 5:
            continue
        entry = F.o[s, k0] * (1 + SLIP)
        risk = (entry - stop) / entry
        if risk < 0.001 or risk > MAX_STOP + 1e-9:
            continue
        size = min(1 / 3, RISK / risk)
        end = min(k0 + HOLD, F.n - 5) if before_close is None else F.n - before_close
        target = entry + tgt_r * (entry - stop) if tgt_r else None
        raw, why = walk_exit(F, s, k0, entry, stop if stop_kind else None, target, end, be_at, trail)
        r = net(entry, raw)
        out.append({"day": F.day, "stock": s, "minute": m, "entry": entry, "raw": raw, "why": why,
                    "net": r, "acct": size * r})
    return out


def exit_stats(trades):
    if not trades:
        return {"n": 0, "wr": 0.0, "total": 0.0}
    r = np.array([t["net"] for t in trades])
    acct = np.array([t["acct"] for t in trades])
    win, loss = r[r > 0], r[r <= 0]
    return {"n": len(r), "wr": (r > 0).mean(), "avg": r.mean(), "win": win.mean() if len(win) else 0.0,
            "loss": loss.mean() if len(loss) else 0.0, "worst": r.min(), "total": acct.sum()}


def exit_line(st):
    if not st["n"]:
        return "no trades"
    return (f"{st['n']:3d} trades | {st['wr'] * 100:3.0f}% winners | avg win {st['win'] * 100:+.2f}% | "
            f"avg loss {st['loss'] * 100:+.2f}% | worst {st['worst'] * 100:+.1f}% | "
            f"total {st['total'] * 100:+6.1f}% of the account")


def exit_versions():
    out = [(15, "1:55 PM", NOW_RULE, None)]
    for n_or in (15, 5):
        for until in LAST_ENTRIES:
            for exit_label in EXIT_TIMES:
                for rule in EXIT_RULES:
                    out.append((n_or, until, rule, exit_label))
    return out


def exits_research():
    symbols, today, feats, tune, test = load_study()
    results = []
    for n_or, until, rule, exit_label in exit_versions():
        before = None if exit_label is None else EXIT_TIMES[exit_label]
        a = [t for F in tune for t in exits_day(F, n_or, LAST_ENTRIES[until], rule, before)]
        b = [t for F in test for t in exits_day(F, n_or, LAST_ENTRIES[until], rule, before)]
        results.append((n_or, until, rule, exit_label, exit_stats(a), exit_stats(b)))
    print(f"Tested {len(results)} versions.")

    def name(x):
        hold = "2-hour hold" if x[3] is None else f"sell everything at {x[3]}"
        return f"{x[0]}-min start, last entry {x[1]}, {hold}: {x[2][0]}"

    now = results[0]
    lines = [f"🕐 Hold until the close: {len(results)} versions of the bot's rules on {len(feats)} trading days "
             f"({feats[0].day} to {feats[-1].day}). Sahm costs included. Per trade: % of the money in that trade. "
             "Total: % of the whole account, added up. Times are New York.",
             f"Older months (used to choose): {tune[0].day} to {tune[-1].day}. "
             f"Newer months (never used to choose): {test[0].day} to {test[-1].day}.", "",
             "== The bot today ==", name(now), f"   older: {exit_line(now[4])}", f"   NEWER: {exit_line(now[5])}", ""]
    held = [x for x in results[1:] if x[4]["n"] >= 100]
    lines.append("== Hold-until-close versions, best total in the older months first ==")
    for x in sorted(held, key=lambda x: -x[4]["total"])[:15]:
        lines += [name(x), f"   older: {exit_line(x[4])}", f"   NEWER: {exit_line(x[5])}"]
    lines += ["", "== Highest win rate in the older months =="]
    for x in sorted(held, key=lambda x: -x[4]["wr"])[:8]:
        lines += [name(x), f"   older: {exit_line(x[4])}", f"   NEWER: {exit_line(x[5])}"]
    lines += ["", f"Of {len(held)} hold-until-close versions, {sum(x[4]['total'] > 0 for x in held)} made money in "
                  f"the older months, {sum(x[5]['total'] > 0 for x in held)} in the newer months, "
                  f"{sum(x[4]['total'] > 0 and x[5]['total'] > 0 for x in held)} in both."]
    lines += ["", "== Every version (older | NEWER: winners, total) =="]
    for x in results:
        lines.append(f"{name(x)}: {x[4]['wr'] * 100:.0f}%, {x[4]['total'] * 100:+.1f}% | "
                     f"{x[5]['wr'] * 100:.0f}%, {x[5]['total'] * 100:+.1f}%")

    path = D.write_report(f"research-exits-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"🕐 Hold-until-close test finished ({len(results)} versions). Full report saved in the repo ({path}).")


# ---------------- news study for the current rules (v3: hold until the 3:20 PM sale) ----------------
NEWS_FILE = os.path.join(DATA_DIR, "news.json")
V3_OR, V3_UNTIL, V3_BEFORE = 5, 320, 40  # 5-minute range, entries until 2:50 PM, everything sold at 3:20 PM
NEWS_PAUSE = 0.9  # seconds between news requests per thread: 3 threads stay under Alpaca's 200 a minute
EARN = re.compile(r"earnings|\bEPS\b|guidance|outlook|estimates|\bQ[1-4]\b|quarterly|fiscal (year|quarter)|"
                  r"revenue|results", re.I)
UP = re.compile(r"upgrade|(raises?|boosts?|lifts?|hikes?) (its |the )?(price )?(target|PT)\b|price target (raised|boosted)"
                r"|initiat\w+ .{0,40}(buy|outperform|overweight)", re.I)
DOWN = re.compile(r"downgrade|(cuts?|lowers?|slashes?|trims?) (its |the )?(price )?(target|PT)\b"
                  r"|price target (cut|lowered)|underperform|underweight|sell rating", re.I)
POS = re.compile(r"\b(beats?|tops|surges?|soars?|jumps?|rall(y|ies)|gains?|record|strong(er)?|upgrades?|raises?|"
                 r"boosts?|wins?|approv(al|ed|es)|expands?|growth|higher|bullish|outperform|rises?|climbs?|"
                 r"rebounds?|profit)\b", re.I)
NEG = re.compile(r"\b(miss(es)?|falls?|plunges?|drops?|sinks?|slumps?|tumbles?|cuts?|downgrades?|weak(er)?|"
                 r"lawsuit|sued|probe|investigation|recalls?|delays?|lowers?|warns?|warning|loss(es)?|bearish|"
                 r"underperform|declines?|layoffs?|fraud|halt(s|ed)?)\b", re.I)


def v3_breakouts(F):
    """Every first breakout of the 5 busiest morning stocks under the current rules, earliest first, each with
    what it made if bought (stop mid, sold 40 minutes before the close). Same trades as exits_day for v3,
    before the 3-a-day limit."""
    rule = ("stop mid, no target", "mid", None, None, None)
    p = dict(V2, **{"or": V3_OR, "until": V3_UNTIL, "stop": "mid", "hold": V3_BEFORE + 25})
    out = []
    for m, rank, s, (_, stop), _ in sorted(breakout(F, p), key=lambda x: (x[0], x[1])):
        k0 = m + 1
        if k0 >= F.n - 5:
            continue
        entry = F.o[s, k0] * (1 + SLIP)
        risk = (entry - stop) / entry
        if risk < 0.001 or risk > MAX_STOP + 1e-9:
            continue
        raw, why = walk_exit(F, s, k0, entry, stop, None, F.n - V3_BEFORE)
        r = net(entry, raw)
        out.append({"day": F.day, "s": s, "m": m, "rank": rank, "net": r, "acct": min(1 / 3, RISK / risk) * r,
                    "why": why, "spy": F.c[F.spy, m] / F.prev_close[F.spy] - 1,
                    "when": dt.datetime.combine(F.day, dt.time(9, 30), D.NY) + dt.timedelta(minutes=m + 1)})
    assert rule[1] == "mid"
    return out


def news_window(symbol, start, end):
    """Headlines about the stock published between start and end, newest first (at most 50)."""
    r = D.api("GET", D.DATA_URL + "/v1beta1/news", {"symbols": symbol, "start": D.iso(start), "end": D.iso(end),
                                                    "limit": 50, "sort": "desc"})
    return [(n["created_at"], n.get("headline", "")) for n in (r.get("news") or [])]


def load_news(rows, symbols):
    """{key: [(published, headline), ...]} for the 4 days before each breakout; cached in data/news.json."""
    cache = {}
    if os.path.exists(NEWS_FILE):
        with open(NEWS_FILE) as f:
            cache = json.load(f)
    todo = [r for r in rows if r["key"] not in cache]
    print(f"News: {len(rows) - len(todo)} breakouts cached, fetching {len(todo)}.")

    def get(r):
        time.sleep(NEWS_PAUSE)
        try:
            return r["key"], news_window(symbols[r["s"]], r["when"] - dt.timedelta(hours=96), r["when"])
        except Exception as e:
            print(f"news unavailable for {r['key']}: {e}")
            return r["key"], None

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        for k, (key, items) in enumerate(pool.map(get, todo), 1):
            if items is not None:
                cache[key] = items
            if k % 100 == 0:
                print(f"  fetched {k}/{len(todo)}")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NEWS_FILE, "w") as f:
        json.dump(cache, f)
    return cache


def news_features(items, when, prev_close):
    """What the headlines said before the buy: counts, earnings, analyst moves, tone (positive - negative words)."""
    parsed = [(dt.datetime.fromisoformat(c.replace("Z", "+00:00")), h) for c, h in items]
    h24 = [h for t, h in parsed if when - dt.timedelta(hours=24) <= t <= when]
    fresh = [h for t, h in parsed if prev_close <= t <= when]
    text = " | ".join(h24)
    tone = sum(len(POS.findall(h)) - len(NEG.findall(h)) for h in h24)
    return {"n24": len(h24), "fresh": len(fresh), "earn": bool(EARN.search(text)), "up": bool(UP.search(text)),
            "down": bool(DOWN.search(text)), "tone": tone, "h24": h24}


NEWS_FILTERS = [  # (name, keep this breakout?)
    ("News in the 24 hours before", lambda r: r["n24"] > 0),
    ("No news in the 24 hours before", lambda r: r["n24"] == 0),
    ("Fresh news since the last close", lambda r: r["fresh"] > 0),
    ("No fresh news since the last close", lambda r: r["fresh"] == 0),
    ("3 or more headlines in 24 hours", lambda r: r["n24"] >= 3),
    ("Earnings news", lambda r: r["earn"]),
    ("No earnings news", lambda r: not r["earn"]),
    ("Analyst upgrade or higher price target", lambda r: r["up"]),
    ("No analyst downgrade or lower target", lambda r: not r["down"]),
    ("Positive headlines (tone above 0)", lambda r: r["tone"] > 0),
    ("Not negative headlines (tone 0 or more)", lambda r: r["tone"] >= 0),
    ("Negative headlines (tone below 0)", lambda r: r["tone"] < 0),
    ("S&P 500 up at the buy", lambda r: r["spy"] > 0),
    ("S&P 500 up AND news in 24 hours", lambda r: r["spy"] > 0 and r["n24"] > 0),
    ("S&P 500 up AND fresh news", lambda r: r["spy"] > 0 and r["fresh"] > 0),
    ("S&P 500 up AND not negative headlines", lambda r: r["spy"] > 0 and r["tone"] >= 0),
    ("S&P 500 up AND positive headlines", lambda r: r["spy"] > 0 and r["tone"] > 0),
    ("Fresh news AND positive headlines", lambda r: r["fresh"] > 0 and r["tone"] > 0),
    ("Fresh news AND not negative", lambda r: r["fresh"] > 0 and r["tone"] >= 0),
]


def take_first(day_rows, keep):
    """The bot's daily pick: the first 3 breakouts (in time order) that pass the filter."""
    out = []
    for r in day_rows:
        if len(out) >= 3:
            break
        if keep(r):
            out.append(r)
    return out


def news_research():
    symbols, today, feats, tune, test = load_study()
    days = []
    for i, F in enumerate(feats):
        prev = feats[i - 1] if i else None
        prev_close = (dt.datetime.combine(prev.day, dt.time(9, 30), D.NY) + dt.timedelta(minutes=prev.n)
                      if prev else None)
        rows = v3_breakouts(F)
        for r in rows:
            r["key"] = f"{symbols[r['s']]}|{r['when'].isoformat()}"
            r["part"] = "older" if i < len(tune) else "newer"
            r["prev_close"] = prev_close or r["when"] - dt.timedelta(hours=24)
        days.append(rows)
    every = [r for rows in days for r in rows]
    news = load_news(every, symbols)
    missing = 0
    for r in every:
        items = news.get(r["key"])
        missing += items is None
        r.update(news_features(items or [], r["when"], r["prev_close"]))
    n_old = sum(r["part"] == "older" for r in every)

    def run(keep):
        out = {"older": [], "newer": []}
        for rows in days:
            for r in take_first(rows, keep):
                out[r["part"]].append(r)
        return exit_stats(out["older"]), exit_stats(out["newer"])

    def avg_line(st):
        if not st["n"]:
            return "no trades"
        return f"{exit_line(st)} | avg trade {st['avg'] * 100:+.2f}%"

    base = run(lambda r: True)
    results = [(name, *run(keep)) for name, keep in NEWS_FILTERS]
    lines = [f"📰 News test for the current rules (5-minute start, entries until 2:50 PM, stop in the middle, "
             f"everything sold at 3:20 PM), {len(feats)} trading days ({feats[0].day} to {feats[-1].day}). "
             "Sahm costs included. Per trade: % of the money in that trade. Total: % of the whole account, added up.",
             f"Older months (used to choose): {tune[0].day} to {tune[-1].day}. "
             f"Newer months (never used to choose): {test[0].day} to {test[-1].day}.",
             f"Breakouts studied: {len(every)} ({n_old} older, {len(every) - n_old} newer); "
             f"news missing for {missing}. Headlines: Benzinga via Alpaca, only those published before the buy.", "",
             "== The bot today (no news filter) ==", f"   older: {avg_line(base[0])}", f"   NEWER: {avg_line(base[1])}",
             "", "== The bot with each news filter (it buys the first 3 breakouts a day that pass) =="]
    for name, a, b in sorted(results, key=lambda x: -x[1]["total"]):
        lines += [name, f"   older: {avg_line(a)}", f"   NEWER: {avg_line(b)}"]
    better = [x for x in results if x[1]["n"] >= 60 and x[2]["n"] >= 25
              and x[1]["avg"] > base[0]["avg"] and x[2]["avg"] > base[1]["avg"]]
    lines += ["", "== Better average trade than the bot today in BOTH periods (at least 60 older / 25 newer trades) =="]
    lines += [f"{n}: older {a['avg'] * 100:+.2f}% vs {base[0]['avg'] * 100:+.2f}% | "
              f"NEWER {b['avg'] * 100:+.2f}% vs {base[1]['avg'] * 100:+.2f}%" for n, a, b in better] or ["none"]
    both = [x for x in results if x[1]["n"] >= 60 and x[2]["n"] >= 25 and x[1]["total"] > 0 and x[2]["total"] > 0]
    lines += ["", "== Made money in BOTH periods =="]
    lines += [f"{n}: older {a['total'] * 100:+.1f}%, NEWER {b['total'] * 100:+.1f}% of the account"
              for n, a, b in both] or ["none"]

    lines += ["", "== Every breakout, bought or not (no 3-a-day limit): average trade by news group =="]
    for name, keep in [("Everything", lambda r: True)] + NEWS_FILTERS:
        cells = []
        for part in ("older", "newer"):
            g = np.array([r["net"] for r in every if r["part"] == part and keep(r)])
            if len(g):
                se = g.std(ddof=1) / np.sqrt(len(g)) if len(g) > 1 else 0
                cells.append(f"{part}: {len(g)} trades, {(g > 0).mean() * 100:.0f}% winners, "
                             f"avg {g.mean() * 100:+.2f}% (± {se * 100:.2f})")
            else:
                cells.append(f"{part}: none")
        lines.append(f"{name}: " + " | ".join(cells))

    lines += ["", "== Examples, to check how headlines were read =="]
    rng = np.random.default_rng(7)
    for label, test_fn in [("earnings", lambda r: r["earn"]), ("analyst up", lambda r: r["up"]),
                           ("analyst down", lambda r: r["down"]), ("positive", lambda r: r["tone"] > 0),
                           ("negative", lambda r: r["tone"] < 0)]:
        pool = [r for r in every if test_fn(r)]
        lines.append(f"{label} ({len(pool)} breakouts):")
        for k in rng.choice(len(pool), size=min(4, len(pool)), replace=False) if pool else []:
            r = pool[int(k)]
            lines.append(f"   {r['day']} {symbols[r['s']]} tone {r['tone']:+d}: {r['h24'][0][:110] if r['h24'] else ''}")

    path = D.write_report(f"research-news-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"📰 News test finished ({len(results)} news filters on {len(every)} breakouts). "
           f"Claude will send you the summary. Full report saved in the repo ({path}).")


if __name__ == "__main__":
    job = sys.argv[1] if len(sys.argv) > 1 else "research"
    try:
        {"research_ai": ai_research, "research_market": market_research,
         "research_hold": hold_research, "research_exits": exits_research,
         "research_news": news_research}.get(job, research)()
    except Exception as e:
        D.send(f"⚠️ Research error: {e}")
        raise
