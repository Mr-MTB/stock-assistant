"""Offline checks for timing, reports and the backtest (fake Alpaca, fake clock).

Run from the repo root:  python3 tests/scenarios.py
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake as F  # noqa: E402

D = F.D
NY = D.NY
SYMS = ["NVDA", "AAPL", "MSFT", "AMD", "KO"]
D.WATCHLIST = SYMS
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def fresh_dir():
    d = tempfile.mkdtemp(prefix="bot-")
    os.chdir(d)
    return d


def at(day, h, m):
    return dt.datetime.combine(day, dt.time(h, m), NY)


up_nvda = lambda s, d: {"NVDA": "up"}.get(s, "chop")  # noqa: E731
DAY = dt.date(2026, 10, 5)  # a Monday (New York summer time)

# 1. Cron run on time at 1:41 AM New York: must wait, restart once, send nothing, change nothing.
fresh_dir()
w = F.World([DAY], SYMS, seed=4, scenario=up_nvda)
F.install(w, at(DAY, 1, 41))
F.run_main()
book = D.load_book()
check("1 early run: one restart requested", len(w.dispatches) == 1, w.dispatches)
check("1 early run: restart within the 6-hour run",
      w.dispatches and w.dispatches[0]["at"] <= at(DAY, 1, 41) + dt.timedelta(minutes=D.JOB_LIMIT_MIN))
check("1 early run: restart body asks for live on main",
      w.dispatches and w.dispatches[0]["body"] == {"ref": "main", "inputs": {"mode": "live"}})
check("1 early run: restart URL", w.dispatches and w.dispatches[0]["url"].endswith(
    "/repos/Mr-MTB/stock-assistant/actions/workflows/assistant.yml/dispatches"))
check("1 early run: uses the run's token", w.dispatches and w.dispatches[0]["auth"] == "Bearer test-token")
check("1 early run: no Telegram messages", not F.SENT, F.SENT)
check("1 early run: day not marked as handled", book.get("last_run") is None, book)
check("1 early run: no report written", not os.path.exists("reports"))
first_restart = w.dispatches[0]["at"]

# 2. The fresh run starts right after (e.g. 7:11 AM): still too early, so it waits until 7:55 and restarts.
w.dispatches.clear()
F.install(w, first_restart + dt.timedelta(seconds=40))
F.run_main()
check("2 second hop: one more restart", len(w.dispatches) == 1, w.dispatches)
go = D.work_end_time(DAY, at(DAY, 16, 0)) - dt.timedelta(minutes=D.JOB_LIMIT_MIN)
check("2 second hop: restarts at the earliest workable time (7:55)",
      w.dispatches and abs((w.dispatches[0]["at"] - go).total_seconds()) < 31, w.dispatches and w.dispatches[0]["at"])
check("2 second hop: still silent", not F.SENT, F.SENT)

# 3. The run started at that moment trades the whole day inside its 6-hour limit.
w.dispatches.clear()
start3 = w.now + dt.timedelta(seconds=30)
F.install(w, start3)
F.run_main()
book = D.load_book()
check("3 on-time run: no further restart", not w.dispatches, w.dispatches)
check("3 on-time run: day marked as handled", book.get("last_run") == DAY.isoformat(), book.get("last_run"))
check("3 on-time run: made the NVDA trade", [t["symbol"] for t in book["trades"]] == ["NVDA"], book["trades"])
check("3 on-time run: finished within the 6-hour limit",
      w.now <= start3 + dt.timedelta(minutes=D.JOB_LIMIT_MIN), w.now)
rep = open(f"reports/live-{DAY}.txt").read() if os.path.exists(f"reports/live-{DAY}.txt") else ""
check("3 on-time run: report has start time, watchlist, buy and sell",
      all(k in rep for k in ("Run started", "watching for breakouts", "BOUGHT NVDA", "SOLD NVDA")), rep[:400])
check("3 on-time run: no secrets in report", "test-token" not in rep)
check("3 on-time run: pick shows a success rate (none measured yet)",
      any("BOUGHT NVDA" in m and "Past success rate: not measured yet." in m for m in F.SENT), F.SENT)
check("3 on-time run: end-of-day result sent",
      any(m.startswith("📊 End of day") and "Today: 1 trade," in m and "Since start:" in m for m in F.SENT), F.SENT)
check("3 on-time run: account starts at 2,000 SAR", abs(book["start_usd"] * D.SAR_PER_USD - 2000) < 0.1, book)

# 4. A late wake-up the same afternoon stops silently (reads the newest trade log).
F.install(w, at(DAY, 15, 2))
before = open("daytrades.json").read()
F.run_main()
check("4 later run same day: silent", not F.SENT, F.SENT)
check("4 later run same day: no restart", not w.dispatches)
check("4 later run same day: trade log unchanged", open("daytrades.json").read() == before)

# 5. Every run late (after 11:30): exactly one notice, then silence.
fresh_dir()
w = F.World([DAY], SYMS, seed=4, scenario=up_nvda)
F.install(w, at(DAY, 14, 27))
F.run_main()
check("5 too late: one notice", len(F.SENT) == 1 and "too late" in F.SENT[0], F.SENT)
check("5 too late: notice shows Saudi times, 12-hour", F.SENT and "9:27 PM" in F.SENT[0] and "6:30 PM" in F.SENT[0], F.SENT)
check("5 too late: report saved", os.path.exists(f"reports/live-{DAY}.txt"))
F.install(w, at(DAY, 15, 10))
F.run_main()
check("5 too late: second late run is silent", not F.SENT, F.SENT)

# 6. Late but before the cutoff (10:05 AM): trades straight away with the time left.
fresh_dir()
w = F.World([DAY], SYMS, seed=4, scenario=up_nvda)
F.install(w, at(DAY, 10, 5))
F.run_main()
book = D.load_book()
check("6 10:05 start: no restart", not w.dispatches)
check("6 10:05 start: handled today", book.get("last_run") == DAY.isoformat())
check("6 10:05 start: sent the watchlist", any("watching for breakouts" in m for m in F.SENT), F.SENT)

# 7. Holiday: silent, nothing saved.
fresh_dir()
w = F.World([DAY - dt.timedelta(days=1), DAY], SYMS, seed=4, holidays=(DAY,))
F.install(w, at(DAY, 8, 0))
F.run_main()
check("7 holiday: silent", not F.SENT and not w.dispatches and not os.path.exists("daytrades.json"))

# 8. Early-close day (1:00 PM close): plans around the shorter day.
fresh_dir()
HALF = dt.date(2026, 11, 27)  # day after Thanksgiving, winter time
w = F.World([HALF], SYMS, seed=4, scenario=up_nvda, close=dt.time(13, 0))
F.install(w, at(HALF, 4, 0))
F.run_main()
go_half = D.work_end_time(HALF, at(HALF, 13, 0)) - dt.timedelta(minutes=D.JOB_LIMIT_MIN)
check("8 half day: last entry 10:55", D.last_entry_time(HALF, at(HALF, 13, 0)) == at(HALF, 10, 55))
check("8 half day: restart at 7:20",
      w.dispatches and abs((w.dispatches[0]["at"] - go_half).total_seconds()) < 31 and go_half == at(HALF, 7, 20),
      (w.dispatches, go_half))
w.dispatches.clear()
start8 = w.now + dt.timedelta(seconds=30)
F.install(w, start8)
F.run_main()
check("8 half day: trades and finishes before the close",
      D.load_book().get("last_run") == HALF.isoformat() and w.now <= at(HALF, 13, 0), w.now)

# 9. On your own computer (not GitHub): no restarts, it just waits and trades.
fresh_dir()
w = F.World([DAY], SYMS, seed=4, scenario=up_nvda)
F.install(w, at(DAY, 1, 41), on_github=False)
F.run_main()
check("9 own computer: no restart, traded", not w.dispatches and D.load_book().get("last_run") == DAY.isoformat())

# 10. Restart refused by GitHub: user is told once.
fresh_dir()
w = F.World([DAY], SYMS, seed=4, scenario=up_nvda)
F.install(w, at(DAY, 1, 41))
orig = F.urllib.request.urlopen


def refuse(req, timeout=None, data=None):
    raise OSError("403 Resource not accessible by integration")


F.urllib.request.urlopen = refuse
F.run_main()
check("10 restart refused: one warning", len(F.SENT) == 1 and "couldn't restart" in F.SENT[0], F.SENT)
F.urllib.request.urlopen = orig

# 11. Backtest: report file with summary, every trade and day-by-day lines.
fresh_dir()
days = [d for d in (dt.date(2026, 8, 3) + dt.timedelta(days=i) for i in range(30)) if d.weekday() < 5][:15]
w = F.World(days, SYMS, seed=11)
F.install(w, at(days[-1] + dt.timedelta(days=3), 8, 0))
w.now = None  # backtest sees all bars
D.now_ny = lambda: dt.datetime.combine(days[-1] + dt.timedelta(days=3), dt.time(8), NY)
D.BACKTEST_DAYS = 15
F.run_main("backtest")
rp = f"reports/backtest-{days[-1] + dt.timedelta(days=3)}.txt"
txt = open(rp).read() if os.path.exists(rp) else ""
check("11 backtest: report saved", bool(txt), rp)
check("11 backtest: has summary, trades and days",
      all(k in txt for k in ("Day-trading backtest", "Every trade", "Day by day", "passed the filters")), txt[:300])
n_day_lines = sum(1 for line in txt.splitlines() if "passed the filters" in line and line[:4] == "2026")
check("11 backtest: one line per day", n_day_lines == 15, n_day_lines)
check("11 backtest: Telegram summary has no per-trade list", F.SENT and "Every trade" not in F.SENT[-1])
st = json.load(open(D.STATS_FILE)) if os.path.exists(D.STATS_FILE) else {}
check("11 backtest: success rate saved", st.get("trades") == 14 and st.get("wins") == 13, st)
check("11 backtest: picks now show it", D.success_text().startswith("Past success rate of this setup: 93% (13 of 14"),
      D.success_text())

# 13. A day with candidates but no breakout: end-of-day says no trade.
class Sliding(F.World):
    """After the first 15 minutes every stock slides steadily, so nothing ever breaks out."""

    def make_day(self, s, d, prev, scenario):
        bars = super().make_day(s, d, prev, lambda *_: "up")
        ref = bars[14]["c"]
        for i, b in enumerate(bars[15:], start=15):
            for k in "ohlc":
                b[k] = ref * (1 - 0.0005 * (i - 14))
        return bars


fresh_dir()
w = Sliding([DAY], SYMS, seed=4)
F.install(w, at(DAY, 9, 0))
F.run_main()
check("13 no-trade day: end-of-day message", any("Today: no trade, so no gain or loss." in m for m in F.SENT), F.SENT)

# 12. Notify mode: sends exactly the given text, and nothing when it's empty.
fresh_dir()
F.install(w, at(DAY, 12, 0))
os.environ["MESSAGE"] = "  📋 Claude: test update  "
D.run_notify()
check("12 notify: sends the message", F.SENT == ["📋 Claude: test update"], F.SENT)
F.SENT.clear()
os.environ["MESSAGE"] = "   "
D.run_notify()
check("12 notify: empty message sends nothing", not F.SENT, F.SENT)
os.environ.pop("MESSAGE", None)

# 14. Telegram self-check: finds missing/wrong secrets, never writes them into the report.
import io  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402

GOOD_TOKEN, GOOD_CHAT = "8000000001:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKl", "5025545947"
calls = []


def fake_telegram(req, timeout=None, data=None):
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    body = req.data if isinstance(req, urllib.request.Request) else data
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
    leaked = any(x.strip() and x.strip() in text for x in (raw_token, raw_chat))
    return text, leaked


txt, leaked = tg_check("", GOOD_CHAT)
check("14 telegram: missing token reported, nothing called", "TELEGRAM_TOKEN: MISSING" in txt and not calls, txt)
txt, leaked = tg_check(GOOD_TOKEN + "\n", " " + GOOD_CHAT)
check("14 telegram: stray spaces/line breaks spotted and fixed",
      txt.count("had extra spaces or line breaks") == 2 and "Test message delivered: yes" in txt, txt)
check("14 telegram: no secret in the report (fixed case)", not leaked)
txt, leaked = tg_check("8000000001:WRONGwrongWRONGwrongWRONGwrong12345", GOOD_CHAT)
check("14 telegram: wrong token reported", "Token accepted by Telegram: NO (Unauthorized)" in txt, txt)
check("14 telegram: no secret in the report (wrong token)", not leaked)
txt, leaked = tg_check(GOOD_TOKEN, "12345")
check("14 telegram: wrong chat ID reported", "Test message delivered: NO (Bad Request: chat not found)" in txt, txt)
txt, leaked = tg_check(GOOD_TOKEN, GOOD_CHAT)
check("14 telegram: all good", "Token accepted by Telegram: yes (@basaqr_stocks_bot)" in txt
      and "Test message delivered: yes" in txt, txt)

# 15. A failed send is written into the day's report (without the token).
fresh_dir()
F.install(w, at(DAY, 12, 0))
F.urllib.request.urlopen = fake_telegram
D.RAW_TG_TOKEN = D.TG_TOKEN = "8000000001:WRONGwrongWRONGwrongWRONGwrong12345"
D.RAW_TG_CHAT = D.TG_CHAT = GOOD_CHAT
D.LOG.clear()
F.ORIG_SEND("hello")
check("15 failed send is recorded", any("(Telegram failed: Unauthorized)" in x for x in D.LOG), D.LOG)
check("15 no token in the record", not any(D.TG_TOKEN in x for x in D.LOG))
D.RAW_TG_TOKEN = D.TG_TOKEN = D.RAW_TG_CHAT = D.TG_CHAT = ""

# 16. One-off later cutoff: a hand-started session can trade after 11:30 on a day already handled,
#     still exits within 2 hours and before the close, and respects the daily trade limit.
class LateBreak(F.World):
    """Quiet until 11:50, then one stock breaks above its opening range."""

    def make_day(self, s, d, prev, scenario):
        bars = super().make_day(s, d, prev, lambda *_: "up")
        top = max(b["h"] for b in bars[:15])
        for i, b in enumerate(bars[15:], start=15):
            level = top * 0.998 if (i < 140 or s != "NVDA") else top * (1 + 0.0004 * (i - 139))
            for k in "ohlc":
                b[k] = level
        return bars


fresh_dir()
w = LateBreak([DAY], SYMS, seed=4)
F.install(w, at(DAY, 11, 41))
D.save_book({"start_usd": 533.33, "balance_usd": 533.33, "paused": False, "last_run": DAY.isoformat(), "trades": []})
os.environ["LAST_ENTRY_NY"] = "13:55"
F.run_main()
book = D.load_book()
check("16 extra session: watches until 8:55 PM your time", any("until 8:55 PM (your time)" in m for m in F.SENT), F.SENT[:1])
check("16 extra session: trades after 11:30", [t["symbol"] for t in book["trades"]] == ["NVDA"], book["trades"])
check("16 extra session: closed before the market close", w.now <= at(DAY, 16, 0), w.now)
check("16 extra session: end-of-day result sent", any(m.startswith("📊 End of day") for m in F.SENT))
F.install(w, at(DAY, 12, 30))
F.run_main()
check("16 extra session: daily trade limit respected", not F.SENT and len(D.load_book()["trades"]) == 1, F.SENT)
os.environ["LAST_ENTRY_NY"] = "1:55 PM"
F.install(w, at(DAY, 12, 31))
F.run_main()
check("16 bad cutoff value is ignored safely", not F.SENT, F.SENT)
os.environ.pop("LAST_ENTRY_NY", None)

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
