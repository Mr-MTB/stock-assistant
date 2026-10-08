"""Offline check of the news study (research.py research_news). No network.

1. The study's copy of the current rules (v3) makes exactly the same trades as the hold-until-close test
   (exits_day with a 5-minute start, entries until 2:50 PM, stop in the middle, sold at 3:20 PM).
2. Headlines are read correctly: only news published before the buy counts, "fresh" means since the
   previous close, and earnings, analyst moves and tone are recognised.
3. The full study runs, writes its report, asks for news only up to each buy time, and reuses its cache.

Run from the repo root:  python3 tests/news_check.py
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
UTC = dt.timezone.utc
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


os.chdir(tempfile.mkdtemp(prefix="news-"))
days = [d for d in (dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(60)) if d.weekday() < 5][:40]
world = MixedWorld(days, SYMS + ["SPY"], seed=7)
F.install(world, dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY))
world.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)

# Fake news service: answers like Alpaca (published between start and end, newest first, at most `limit`).
NEWS, CALLS = {}, []
real_api = D.api


def news_api(method, url, params=None, body=None):
    if url.endswith("/v1beta1/news"):
        CALLS.append(dict(params))
        st = dt.datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        en = dt.datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        items = sorted((x for x in NEWS.get(params["symbols"], []) if st <= x[0] <= en), reverse=True)
        return {"news": [{"created_at": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "headline": h}
                         for t, h in items[:int(params["limit"])]]}
    return real_api(method, url, params, body)


D.api = news_api
R.NEWS_PAUSE = 0

# 1. Same trades as the hold-until-close test's version of the current rules
cal = D.calendar(days[0], days[-1])
symbols = R.syms()
raw = R.load_days(cal, symbols)
table = R.daily_table(symbols, cal)
feats = [R.Day(day, raw[day], R.day_stats(table, symbols, day)) for day, _, _ in cal]
v3_rule = ("stop mid, no target", "mid", None, None, None)
same, total, n_trades = 0, 0, 0
for F_ in feats:
    a = [(r["s"], round(r["net"], 9), round(r["acct"], 9)) for r in R.take_first(R.v3_breakouts(F_), lambda r: True)]
    b = [(t["stock"], round(t["net"], 9), round(t["acct"], 9)) for t in R.exits_day(F_, 5, 320, v3_rule, 40)]
    total += 1
    same += a == b
    n_trades += len(b)
check("v3: same trades as the hold-until-close test on every day", same == total, f"{same}/{total}")
check("v3: enough trades to mean something", n_trades >= 30, n_trades)

# 2. Reading headlines
when = dt.datetime(2026, 6, 10, 10, 0, tzinfo=NY)
prev_close = dt.datetime(2026, 6, 9, 16, 0, tzinfo=NY)


def z(y, mo, d, h, mi):  # a New York time as Alpaca's UTC text
    return dt.datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


items = [(z(2026, 6, 10, 10, 30), "Acme plunges after downgrade"),             # after the buy: ignored
         (z(2026, 6, 10, 9, 15), "Acme beats Q2 earnings estimates, raises guidance"),
         (z(2026, 6, 9, 14, 0), "Analyst upgrades Acme to Buy"),               # before yesterday's close
         (z(2026, 6, 8, 11, 0), "Acme falls on lawsuit")]                       # more than 24 hours before
f = R.news_features(items, when, prev_close)
check("news: only headlines before the buy and within 24 hours", f["n24"] == 2, f)
check("news: fresh = since yesterday's close", f["fresh"] == 1, f)
check("news: earnings and analyst upgrade recognised, no downgrade", f["earn"] and f["up"] and not f["down"], f)
check("news: 'beats estimates, raises outlook' counts as earnings news",
      R.news_features([(z(2026, 6, 10, 8, 0), "Acme beats estimates, raises outlook")], when, prev_close)["earn"])
check("news: tone counts positive words (beats, raises, upgrades)", f["tone"] == 3, f)
g = R.news_features([(z(2026, 6, 10, 8, 0), "Goldman cuts price target on Acme, shares drop")], when, prev_close)
check("news: lower price target is a downgrade, tone negative", g["down"] and not g["up"] and g["tone"] == -2, g)
h = R.news_features([], when, prev_close)
check("news: no headlines", h["n24"] == 0 and h["fresh"] == 0 and h["tone"] == 0 and not h["earn"], h)

# 3. Full study: every breakout gets headlines up to its buy time only; the cache is reused.
rows = [r for F_ in feats for r in R.v3_breakouts(F_)]
for k, r in enumerate(rows):
    s = symbols[r["s"]]
    NEWS.setdefault(s, []).append((r["when"] - dt.timedelta(hours=2), f"{s} beats estimates and raises outlook"
                                   if k % 3 else f"{s} falls after analyst downgrade"))
    NEWS[s].append((r["when"] + dt.timedelta(minutes=5), f"{s} news after the buy"))
F.SENT.clear()
R.news_research()
path = f"reports/research-news-{D.now_ny().date()}.txt"
text = open(path).read() if os.path.exists(path) else ""
check("study: report has every section", all(k in text for k in (
    "The bot today (no news filter)", "each news filter", "BOTH periods", "Every breakout, bought or not",
    "Examples")), text[:400])
check("study: every news filter listed", all(name in text for name, _ in R.NEWS_FILTERS))
check("study: one news request per breakout", len(CALLS) == len(rows), (len(CALLS), len(rows)))
whens = {D.iso(r["when"]) for r in rows}
check("study: news asked only up to each buy time", all(c["end"] in whens for c in CALLS))
check("study: headlines after the buy never used", "news after the buy" not in text)
check("study: Telegram note sent", any("News test finished" in m for m in F.SENT), F.SENT[-1:])
check("study: no breakout is missing its news", "news missing for 0" in text, text[:600])
CALLS.clear()
R.news_research()
check("study: second run uses the saved news, no new requests", not CALLS, len(CALLS))

print("\nALL PASSED" if ok_all else "\nSOME CHECKS FAILED")
sys.exit(0 if ok_all else 1)
