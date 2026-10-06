"""Offline checks for the live bot: timing, trading rules, hand-overs, messages and the backtest.
Fake Alpaca, fake clock, no network.

Run from the repo root:  python3 tests/scenarios.py
"""
import datetime as dt
import io
import csv
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake as F  # noqa: E402

D = F.D
NY = D.NY
SYMS = ["NVDA", "AAPL", "MSFT", "AMD", "KO", "XOM", "PEP"]
D.WATCHLIST = SYMS
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def fresh_dir():
    d = tempfile.mkdtemp(prefix="bot-")
    os.chdir(d)
    return d


def at(day, h, m, s=0):
    return dt.datetime.combine(day, dt.time(h, m, s), NY)


def sent(text):
    return [m for m in F.SENT if text in m]


def journal(name):
    path = f"journal/{name}.csv"
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []


DAY = dt.date(2026, 10, 5)  # a Monday (New York summer time)
SLOT_USD = 2000 / D.SAR_PER_USD / 3

# A normal day: four stocks break out (10:10, 10:30, 11:00, 11:30); a fifth never does.
# NVDA keeps rising, AAPL falls through its stop, MSFT goes flat, AMD would rise.
NORMAL = {"NVDA": (40, "up"), "AAPL": (60, "stop"), "MSFT": (90, "time"), "AMD": (120, "up"),
          "KO": (None, "time")}
GO = (9, 28, 30)  # the trading run is up at 9:28 AM

# 1. Cron run on time at 1:41 AM: wait, hand over once, send nothing, save nothing.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
F.install(w, at(DAY, 1, 41))
F.run_main()
check("1 early run: one restart requested", len(w.dispatches) == 1, w.dispatches)
check("1 early run: restart inside its 6-hour limit",
      w.dispatches and w.dispatches[0]["at"] <= at(DAY, 1, 41) + dt.timedelta(minutes=D.JOB_LIMIT_MIN))
check("1 early run: restart asks for live on main",
      w.dispatches and w.dispatches[0]["body"] == {"ref": "main", "inputs": {"mode": "live"}})
check("1 early run: restart URL", w.dispatches and w.dispatches[0]["url"].endswith(
    "/repos/Mr-MTB/stock-assistant/actions/workflows/assistant.yml/dispatches"))
check("1 early run: uses the run's token", w.dispatches and w.dispatches[0]["auth"] == "Bearer test-token")
check("1 early run: silent, nothing saved", not F.SENT and not os.path.exists("daytrades.json"), F.SENT)
first = w.dispatches[0]["at"]

# 2. The fresh run (7:28 AM) waits until 9:28 AM and hands over again.
w.dispatches.clear()
F.install(w, first + dt.timedelta(seconds=40))
F.run_main()
check("2 second hop: restart at 9:28 AM", len(w.dispatches) == 1 and
      abs((w.dispatches[0]["at"] - at(DAY, 9, 28)).total_seconds()) < 31, w.dispatches)
check("2 second hop: silent", not F.SENT, F.SENT)

# 3. The 9:28 AM run trades the day: first 3 breakouts, a third of the money each, held to the 3:20 PM sale.
w.dispatches.clear()
w.news = {"NVDA": ["Nvidia unveils a new AI chip", "Analysts raise Nvidia targets"]}
start3 = at(DAY, *GO)
F.install(w, start3)
F.run_main()
book = D.load_book()
trades = book["trades"]
check("3 normal day: no hand-over needed", not w.dispatches, w.dispatches)
by_stock = {t["symbol"]: t for t in trades}  # trades are logged in the order they close
check("3 normal day: bought the first 3 breakouts, in order",
      [m.split()[2] for m in sent("BOUGHT")] == ["NVDA", "AAPL", "MSFT"], sent("BOUGHT"))
check("3 normal day: AAPL stopped out, NVDA and MSFT sold at the end of the day",
      {k: v["exit_reason"] for k, v in by_stock.items()} == {"NVDA": "time", "AAPL": "stop", "MSFT": "time"},
      {k: v["exit_reason"] for k, v in by_stock.items()})
check("3 normal day: the riser wins, the faller and the flat one lose",
      {k: v["pnl_usd"] > 0 for k, v in by_stock.items()} == {"NVDA": True, "AAPL": False, "MSFT": False},
      {k: v["pnl_usd"] for k, v in by_stock.items()})
check("3 normal day: end-of-day sales at 3:20 PM (10:20 PM your time)",
      len(sent("(end-of-day sale)")) == 2 and len(sent("SOLD NVDA")) == 1 and
      "[3:20 PM New York" in [x for x in open(f"reports/live-{DAY}.txt").read().split("\n") if "SOLD NVDA" in x][0],
      sent("SOLD"))
check("3 normal day: 15-minute warning before the sale",
      any("15 minutes left before the end-of-day sale (10:20 PM your time)" in m for m in F.SENT), sent("⏳"))
check("3 normal day: AMD skipped (3 trades already)", not sent("BOUGHT AMD"))
check("3 normal day: each trade about a third of the money",
      all(abs(t["qty"] * t["entry"] - SLOT_USD) < 0.02 for t in trades), [t["qty"] * t["entry"] for t in trades])
check("3 normal day: AAPL bought while NVDA was still open",
      sent("BOUGHT AAPL") and F.SENT.index(sent("BOUGHT AAPL")[0]) < F.SENT.index(sent("SOLD NVDA")[0]))
check("3 normal day: nothing left open", book["open"] == [] and book["day"]["done"], book.get("open"))
check("3 normal day: balance adds up",
      abs(book["balance_usd"] - (book["start_usd"] + sum(t["pnl_usd"] for t in trades))) < 0.02)
watch = sent("picks: watching for breakouts")
check("3 normal day: one watchlist, 12-hour times, 5 picks, market and success rate",
      len(watch) == 1 and "until 9:50 PM (your time)" in watch[0] and "5. KO" in watch[0]
      and "Market (S&P 500): +" in watch[0] and "Past success rate" in watch[0], watch)
check("3 normal day: news mark on NVDA only", watch and "NVDA: buy above" in watch[0] and
      watch[0].split("\n")[2].endswith("📰") and not watch[0].split("\n")[3].endswith("📰"), watch)
b_nvda, b_aapl = sent("BOUGHT NVDA"), sent("BOUGHT AAPL")
check("3 normal day: buy message has trade number, no fixed target, market, headlines, success rate",
      b_nvda and "(trade 1 of 3 today)" in b_nvda[0] and "Market (S&P 500)" in b_nvda[0]
      and "No fixed target: it rides until 10:20 PM (your time) unless the stop is hit" in b_nvda[0]
      and "• Nvidia unveils a new AI chip" in b_nvda[0] and "Past success rate" in b_nvda[0], b_nvda)
check("3 normal day: no-news message", b_aapl and "No news about it in the last 24 hours" in b_aapl[0], b_aapl)
eod = sent("End of day")
check("3 normal day: one end-of-day message with 3 trades",
      len(eod) == 1 and "Mon Oct 5" in eod[0] and "Today: 3 trades," in eod[0], eod)
rep = open(f"reports/live-{DAY}.txt").read()
check("3 normal day: report has start time and all trades",
      all(k in rep for k in ("Run started at 9:28 AM", "SOLD NVDA", "SOLD AAPL", "SOLD MSFT")), rep[:300])
check("3 normal day: finished inside the 6-hour limit", w.now <= start3 + dt.timedelta(minutes=D.JOB_LIMIT_MIN))
jt, js, jd = journal("trades"), journal("signals"), journal("days")
check("3 database: every trade with its context",
      sorted((r["symbol"], r["result"], r["exit_reason"]) for r in jt) ==
      [("AAPL", "loss", "stop"), ("MSFT", "loss", "time"), ("NVDA", "win", "time")]
      and [r for r in jt if r["symbol"] == "NVDA"][0]["had_news"] == "yes"
      and "Nvidia unveils" in [r for r in jt if r["symbol"] == "NVDA"][0]["headlines"]
      and [r for r in jt if r["symbol"] == "AAPL"][0]["had_news"] == "no"
      and all(r["market_pct"] and r["rvol"] and r["range_pct"] and r["entry_time_ny"] for r in jt), jt)
check("3 database: every breakout, bought or not, with what it would have made",
      [(r["symbol"], r["taken"], r["why_not"], r["would_exit"]) for r in js] ==
      [("NVDA", "yes", "", "time"), ("AAPL", "yes", "", "stop"), ("MSFT", "yes", "", "time"),
       ("AMD", "no", "limit", "time")] and js[3]["would_result"] == "win", js)
check("3 database: one row for the day", len(jd) == 1 and jd[0]["candidates"] == "5" and jd[0]["signals"] == "4"
      and jd[0]["trades"] == "3" and jd[0]["wins"] == "1", jd)

# 4. A late wake-up the same afternoon stops silently.
before = open("daytrades.json").read()
F.install(w, at(DAY, 15, 40))
F.run_main()
check("4 later run same day: silent, no restart, log unchanged",
      not F.SENT and not w.dispatches and open("daytrades.json").read() == before, F.SENT)

# 5. Every run late (after 2:50 PM): one notice, then silence.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
F.install(w, at(DAY, 15, 0))
F.run_main()
check("5 too late: one notice in 12-hour Saudi time",
      len(F.SENT) == 1 and "too late (10:00 PM your time)" in F.SENT[0] and "(9:50 PM)" in F.SENT[0], F.SENT)
F.install(w, at(DAY, 15, 10))
F.run_main()
check("5 too late: second late run is silent", not F.SENT, F.SENT)

# 6. Market down more than 1%: no buys, one note per stock that broke out.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, spy="down", seed=4)
F.install(w, at(DAY, *GO))
F.run_main()
book = D.load_book()
check("6 market down: no trades", book["trades"] == [] and not sent("BOUGHT"), book["trades"])
skips = sent("so no buy for now")
check("6 market down: one note per breakout", len(skips) == 4 and len({m.split()[1] for m in skips}) == 4, skips)
check("6 market down: watchlist shows the market", sent("picks") and "Market (S&P 500): -0.50% today" in sent("picks")[0],
      sent("picks"))
check("6 market down: end of day says no trade", sent("Today: no trade, so no gain or loss."))
check("6 database: skipped breakouts recorded with the reason",
      [(r["taken"], r["why_not"]) for r in journal("signals")] == [("no", "market")] * 4
      and journal("days")[0]["trades"] == "0", journal("signals"))

# 7. Hand-over: a run that started early reaches its 6-hour limit with a trade open; the next run carries on.
LATE = {"NVDA": (200, "time")}  # buys 12:51 PM, held to the 3:20 PM sale
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, LATE, seed=4)
F.install(w, at(DAY, *GO), job_start=at(DAY, 7, 30))  # limit reached about 1:27 PM
F.run_main()
book = D.load_book()
check("7 hand-over: fresh run requested", len(w.dispatches) == 1 and w.dispatches[0]["at"] < at(DAY, 13, 30),
      w.dispatches)
check("7 hand-over: open trade saved", [p["symbol"] for p in book["open"]] == ["NVDA"] and "checked" in book["open"][0],
      book.get("open"))
check("7 hand-over: no end of day yet", not sent("End of day"))
w.dispatches.clear()
F.install(w, at(DAY, 13, 30))
F.run_main()
book = D.load_book()
check("7 resume: trade sold at the end-of-day sale", [(t["symbol"], t["exit_reason"]) for t in book["trades"]]
      == [("NVDA", "time")] and book["open"] == [], book["trades"])
check("7 resume: no second watchlist, one end of day",
      not sent("picks: watching") and len(sent("End of day")) == 1, F.SENT)
check("7 resume: sold by 3:21 PM", w.now < at(DAY, 15, 21), w.now)
check("7 database: written once, across the hand-over",
      len(journal("trades")) == 1 and len(journal("signals")) == 1 and len(journal("days")) == 1,
      (journal("trades"), journal("signals"), journal("days")))

# 7b. Hand-over with nothing open, and the next run only starts after 2:50 PM: it wraps up the day.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, {"NVDA": (None, "time")}, seed=4)
F.install(w, at(DAY, *GO), job_start=at(DAY, 7, 30))
F.run_main()
check("7b hand-over before the last entry time", len(w.dispatches) == 1 and not D.load_book()["open"], w.dispatches)
F.install(w, at(DAY, 15, 0))
F.run_main()
check("7b late resume: one end-of-day, no 'too late' notice",
      len(sent("End of day")) == 1 and not sent("too late"), F.SENT)

# 8. Stop reached during the hand-over gap: the next run sees it and sells.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, LATE, dip_at={"NVDA": 238}, seed=4)  # dip at 1:28 PM, while no run is watching
F.install(w, at(DAY, *GO), job_start=at(DAY, 7, 30))
F.run_main()
F.install(w, at(DAY, 13, 31))
F.run_main()
book = D.load_book()
check("8 gap: stop hit while no run was watching is caught",
      [(t["symbol"], t["exit_reason"]) for t in book["trades"]] == [("NVDA", "stop")], book["trades"])

# 9. Hand-over refused by GitHub: close the trade for safety.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, LATE, seed=4)
F.install(w, at(DAY, *GO), job_start=at(DAY, 7, 30))
orig = F.urllib.request.urlopen


def refuse(req, timeout=None, data=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if "api.github.com" in url:
        raise OSError("403 Resource not accessible by integration")
    return orig(req, timeout=timeout, data=data)


F.urllib.request.urlopen = refuse
F.run_main()
F.urllib.request.urlopen = orig
book = D.load_book()
check("9 hand-over refused: trade closed for safety",
      [(t["symbol"], t["exit_reason"]) for t in book["trades"]] == [("NVDA", "safety")] and not book["open"],
      book["trades"])
check("9 hand-over refused: owner told", sent("couldn't hand over") and sent("End of day"), F.SENT)

# 10. Something breaks mid-day: open trades are closed, then the error is reported.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, {"NVDA": (40, "time")}, seed=4)
F.install(w, at(DAY, *GO))
real_api = D.api


def flaky(method, url, params=None, body=None):
    if "/stocks/trades/latest" in url and W_now() >= at(DAY, 10, 30) and "NVDA" in params["symbols"]:
        if not getattr(flaky, "tripped", False):
            flaky.tripped = True
            raise RuntimeError("Alpaca GET /v2/stocks/trades/latest -> 500: boom")
    return real_api(method, url, params, body)


def W_now():
    return F.W.now


D.api = flaky
try:
    F.run_main()
    crashed = False
except RuntimeError:
    crashed = True
D.api = real_api
book = D.load_book()
check("10 error: run stops with the error", crashed)
check("10 error: open trade closed for safety", [t["exit_reason"] for t in book["trades"]] == ["safety"]
      and not book["open"], book["trades"])

# 11. Holiday: silent.
fresh_dir()
w = F.ScriptWorld([DAY - dt.timedelta(days=1), DAY], SYMS, NORMAL, seed=4, holidays=(DAY,))
F.install(w, at(DAY, 9, 20))
F.run_main()
check("11 holiday: silent", not F.SENT and not w.dispatches and not os.path.exists("daytrades.json"))

# 12. Early close (1:00 PM): sale at 12:20 PM, last entry 11:50 AM.
HALF = dt.date(2026, 11, 27)
fresh_dir()
w = F.ScriptWorld([HALF], SYMS, NORMAL, close=dt.time(13, 0), seed=4)
F.install(w, at(HALF, *GO))
F.run_main()
book = D.load_book()
check("12 half day: last entry 11:50 AM, sale at 12:20 PM",
      D.last_entry_time(HALF, at(HALF, 13, 0)) == at(HALF, 11, 50)
      and D.trade_deadline(None, at(HALF, 13, 0)) == at(HALF, 12, 20))
check("12 half day: 3 trades, all sold by 12:21 PM",
      [t["symbol"] for t in book["trades"]] == ["AAPL", "NVDA", "MSFT"] and w.now <= at(HALF, 12, 21),
      (book["trades"], w.now))

# 13. On your own computer: no restarts, it just waits and trades.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
F.install(w, at(DAY, 1, 41), on_github=False)
F.run_main()
check("13 own computer: waited and traded", not w.dispatches and len(D.load_book()["trades"]) == 3)

# 14. News service down: trading carries on.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
w.news_down = True
F.install(w, at(DAY, *GO))
F.run_main()
check("14 news down: still trades, says so", len(D.load_book()["trades"]) == 3 and
      sent("News: couldn't load it right now."), F.SENT[:2])

# 15. Backtest: report, success rate, and the same trades the live bot makes.
fresh_dir()
days = [d for d in (dt.date(2026, 8, 3) + dt.timedelta(days=i) for i in range(30)) if d.weekday() < 5][:15]
w = F.World(days, SYMS + ["SPY"], seed=11)
F.install(w, at(days[-1] + dt.timedelta(days=3), 8, 0))
w.now = None
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)
D.BACKTEST_DAYS = 15
F.run_main("backtest")
rp = f"reports/backtest-{days[-1] + dt.timedelta(days=3)}.txt"
txt = open(rp).read() if os.path.exists(rp) else ""
check("15 backtest: report saved with rules, trades and days",
      all(k in txt for k in ("Day-trading backtest", "up to 3 trades a day", "Every trade", "Day by day")), txt[:300])
check("15 backtest: news and market comparison in the report",
      "Did news or the market make a difference?" in txt and "No news:" in txt and "Market (S&P 500) up at entry:" in txt,
      txt[-600:])
st = json.load(open(D.STATS_FILE)) if os.path.exists(D.STATS_FILE) else {}
check("15 backtest: success rate saved", st.get("trades", 0) > 0 and 0 <= st.get("win_rate", -1) <= 1, st)
check("15 backtest: picks now show it", D.success_text().startswith("Past success rate of this setup:"),
      D.success_text())

agree, total = 0, 0
for seed in range(12):
    one = [dt.date(2026, 9, 21)]
    world = F.World(one, SYMS + ["SPY"], seed=seed)
    fresh_dir()
    F.install(world, at(one[0], *GO))
    F.run_main()
    live = [(t["symbol"], t["exit_reason"]) for t in D.load_book()["trades"]]
    world.now = None
    o, c = at(one[0], 9, 30), at(one[0], 16, 0)
    daily = D.get_bars(SYMS + ["SPY"], "1Day", o - dt.timedelta(days=60), c, "split")
    bars = D.get_bars(SYMS + ["SPY"], "1Min", o, c)
    bt, _, _ = D.simulate_day(bars, D.stats_for_day(daily, one[0]), 533.33, one[0], o, c)
    back = [(t["symbol"], t["exit_reason"]) for t in bt]
    total += 1
    agree += sorted(live) == sorted(back)
    if sorted(live) != sorted(back):
        print("   seed", seed, "live", live, "backtest", back)
check("15 live bot and backtest make the same trades", agree == total, f"{agree}/{total}")

# 16. Notify mode: sends exactly the given text, nothing when empty.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
F.install(w, at(DAY, 12, 0))
os.environ["MESSAGE"] = "  📋 Claude: test update  "
D.run_notify()
check("16 notify: sends the message", F.SENT == ["📋 Claude: test update"], F.SENT)
F.SENT.clear()
os.environ["MESSAGE"] = "   "
D.run_notify()
check("16 notify: empty message sends nothing", not F.SENT, F.SENT)
os.environ.pop("MESSAGE", None)

# 17. Telegram self-check and failed sends never expose the secrets.
GOOD_TOKEN, GOOD_CHAT = "8000000001:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKl", "5025545947"
calls = []


def fake_telegram(req, timeout=None, data=None):
    url = req.full_url if isinstance(req, F.urllib.request.Request) else req
    body = req.data if isinstance(req, F.urllib.request.Request) else data
    token, method = url.split("/bot", 1)[1].split("/", 1)
    calls.append(method)

    def fail(code, why):
        raise urllib.error.HTTPError(url, code, why, {}, io.BytesIO(json.dumps(
            {"ok": False, "error_code": code, "description": why}).encode()))
    if token != GOOD_TOKEN:
        fail(401, "Unauthorized")
    if method == "getMe":
        return F.FakeResp(json.dumps({"ok": True, "result": {"username": "basaqr_stocks_bot"}}).encode())
    if urllib.parse.parse_qs(body.decode())["chat_id"][0] != GOOD_CHAT:
        fail(400, "Bad Request: chat not found")
    return F.FakeResp(b'{"ok": true, "result": {}}')


def tg_check(raw_token, raw_chat):
    fresh_dir()
    F.install(w, at(DAY, 12, 0))
    F.urllib.request.urlopen = fake_telegram
    D.RAW_TG_TOKEN, D.RAW_TG_CHAT = raw_token, raw_chat
    D.TG_TOKEN, D.TG_CHAT = raw_token.strip(), raw_chat.strip()
    calls.clear()
    D.run_telegram_check()
    text = open("reports/telegram-check.txt").read()
    return text, any(x.strip() and x.strip() in text for x in (raw_token, raw_chat))


txt, leaked = tg_check("", GOOD_CHAT)
check("17 telegram: missing token reported, nothing called", "TELEGRAM_TOKEN: MISSING" in txt and not calls, txt)
txt, leaked = tg_check(GOOD_TOKEN + "\n", " " + GOOD_CHAT)
check("17 telegram: stray spaces spotted and fixed, no secret shown",
      txt.count("had extra spaces or line breaks") == 2 and "Test message delivered: yes" in txt and not leaked, txt)
txt, leaked = tg_check("8000000001:WRONGwrongWRONGwrongWRONGwrong12345", GOOD_CHAT)
check("17 telegram: wrong token reported, no secret shown",
      "Token accepted by Telegram: NO (Unauthorized)" in txt and not leaked, txt)
txt, leaked = tg_check(GOOD_TOKEN, "12345")
check("17 telegram: wrong chat ID reported", "Test message delivered: NO (Bad Request: chat not found)" in txt, txt)
fresh_dir()
F.install(w, at(DAY, 12, 0))
F.urllib.request.urlopen = fake_telegram
D.RAW_TG_TOKEN = D.TG_TOKEN = "8000000001:WRONGwrongWRONGwrongWRONGwrong12345"
D.RAW_TG_CHAT = D.TG_CHAT = GOOD_CHAT
D.LOG.clear()
F.ORIG_SEND("hello")
check("17 failed send is recorded without the token",
      any("(Telegram failed: Unauthorized)" in x for x in D.LOG) and not any(D.TG_TOKEN in x for x in D.LOG), D.LOG)
D.RAW_TG_TOKEN = D.TG_TOKEN = D.RAW_TG_CHAT = D.TG_CHAT = ""

# 18b. Late start: breakouts that happened before the run started are skipped, later ones are taken.
fresh_dir()
w = F.ScriptWorld([DAY], SYMS, NORMAL, seed=4)
F.install(w, at(DAY, 10, 35))  # NVDA (10:10) and AAPL (10:30) broke out before; NVDA has run too far since
F.run_main()
book = D.load_book()
check("18b late start: earlier breakouts skipped, later ones bought",
      sorted(t["symbol"] for t in book["trades"]) == ["AMD", "MSFT"] and sent("picks: watching"),
      [t["symbol"] for t in book["trades"]])
check("18b late start: noted in the report", "Started late" in open(f"reports/live-{DAY}.txt").read())

# 18. 12-hour clock.
check("18 12-hour times", [D.clock(dt.datetime(2026, 1, 1, h, m)) for h, m in ((9, 5), (12, 0), (0, 30), (20, 55))]
      == ["9:05 AM", "12:00 PM", "12:30 AM", "8:55 PM"])

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
