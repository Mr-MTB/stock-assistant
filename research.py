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
import datetime as dt
import itertools
import os

import numpy as np

import daytrader as D

np.seterr(all="ignore")  # stocks with missing data give NaN comparisons, which count as "no signal"

DAYS = int(os.environ.get("RESEARCH_DAYS", "250"))
TEST_SHARE = 0.30
MIN_TRADES = 40            # in the tuning months
GOAL_WIN_RATE = 0.80
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
        self.vwap = np.where(cv > 0, np.cumsum(tp * self.v, axis=1) / np.maximum(cv, 1e-12), self.c)
        self.prev_close, self.avg_vol, self.sma20, self.sma50 = (stats[:, k] for k in range(4))
        self.ok = (self.real >= 0.5) & ~np.isnan(self.prev_close) & (self.avg_vol > 0)
        self.spy = arr.shape[0] - 1
        self.ok[self.spy] = False  # SPY is only used as the market filter


def cutoff(F, minute):
    return min(minute, F.n - (HOLD + 5))


# ---------------- rule families ----------------
# Each returns the first signal of every qualifying stock: (minute, tie_rank, stock, stop, target).
# stop/target: ("abs", price) | ("pct", fraction) | ("R", multiple of risk) | ("high", None)
def breakout(F, p):
    """Opening range breakout (the current bot when or=15, stop=mid, tgt=2R, until 11:30, no filters)."""
    n_or, cut = p["or"], cutoff(F, p["until"])
    hi, lo = F.h[:, :n_or].max(1), F.l[:, :n_or].min(1)
    last, vol = F.c[:, n_or - 1], F.v[:, :n_or].sum(1)
    rng = (hi - lo) / last
    ok = F.ok & (vol > 0) & (last > F.vwap[:, n_or - 1]) & (last > F.prev_close) & (rng >= 0.003) & (rng <= 0.03)
    if p["trend"]:
        ok &= F.prev_close > F.sma50
    rvol = np.where(ok, vol / F.avg_vol, -1)
    cands = [s for s in np.argsort(-rvol, kind="stable")[:3] if ok[s]]
    mkt = F.c[F.spy, n_or:cut] > F.vwap[F.spy, n_or:cut]
    sigs = []
    for rank, s in enumerate(cands):
        cc = F.c[s, n_or:cut]
        hit = (cc > hi[s]) & (cc <= hi[s] + 0.5 * (hi[s] - lo[s]))
        if p["mkt"]:
            hit &= mkt
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
def trade_for_day(F, sigs):
    for m, _, s, stop_spec, tgt_spec in sorted(sigs, key=lambda x: (x[0], x[1])):
        k0 = m + 1
        if k0 >= F.n - 5:
            continue
        entry = F.o[s, k0] * (1 + SLIP)
        stop = stop_spec[1] if stop_spec[0] == "abs" else entry * (1 - stop_spec[1])
        risk = (entry - stop) / entry
        if risk < 0.001 or risk > MAX_STOP + 1e-9:
            continue
        kind, val = tgt_spec
        target = (entry + val * (entry - stop) if kind == "R" else entry * (1 + val) if kind == "pct"
                  else F.h[s, :m + 1].max() if kind == "high" else val)
        if target <= entry * 1.001:
            continue
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
        exit_ = raw * (1 - SLIP)
        size = min(D.MAX_POSITION, RISK / risk)
        ret = size * ((exit_ / entry - 1) - FEE - FEE * exit_ / entry)
        best = F.h[s, k0:k_exit + 1].max() / entry - 1  # best price reached while holding
        return {"day": F.day, "stock": s, "minute": m, "entry": entry, "exit": exit_, "why": why,
                "ret": ret, "gross": size * (raw / F.o[s, k0] - 1), "best": best,
                "mkt_up": F.c[F.spy, m] > F.vwap[F.spy, m]}
    return None


def run(days, family, params):
    fn = FAMILIES[family]
    return [t for t in (trade_for_day(F, fn(F, params)) for F in days) if t]


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


def research():
    symbols = syms()
    today = D.now_ny().date()
    days = D.calendar(today - dt.timedelta(days=int(DAYS * 1.6) + 10), today - dt.timedelta(days=1))[-DAYS:]
    raw = load_days(days, symbols)
    table = daily_table(symbols, days)
    feats = [Day(day, raw[day], day_stats(table, symbols, day)) for day, _, _ in days]
    split = int(len(feats) * (1 - TEST_SHARE))
    tune, test = feats[:split], feats[split:]
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

    path = D.write_report(f"research-{today}.txt", lines, mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")
    D.send(f"🔬 Research finished: {len(results)} rule variations tested on {len(feats)} trading days. "
           f"Full report saved in the repo ({path}).")


if __name__ == "__main__":
    try:
        research()
    except Exception as e:
        D.send(f"⚠️ Research error: {e}")
        raise
