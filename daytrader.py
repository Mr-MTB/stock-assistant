"""
Day Trader Bot v1.1 (PAPER trading)
Prepared by: Eng. Mohammed T. Basaqr

Strategy: Opening Range Breakout, long only, every trade closed within 2 hours.
  1. 9:30-9:45 New York time: record each stock's first-15-minute high and low.
  2. Pick up to 3 "stocks in play": unusually high volume, trading up on the day, above VWAP.
  3. Buy when a 1-minute candle closes above the 15-minute high (until 11:30).
  4. Stop = middle of the 15-minute range. Target = 2x the risk. Exit anyway after 2 hours.
  5. Medium risk: lose at most ~3% of the account if the stop hits. Never borrows money.
  6. One trade per day (a cash account can't reuse sale money until the next day).

Modes:
  python daytrader.py live      -> trades one day on the Alpaca PAPER account
  python daytrader.py backtest  -> replays the rules on the last 60 trading days
  python daytrader.py notify    -> sends the text in MESSAGE to Telegram (updates from Claude)

Timing on GitHub: scheduled runs often start hours late, so the workflow has several
wake-up calls. A run that starts too early for one 6-hour GitHub run to cover the trading
day waits, then starts a fresh run at the right moment. A run that starts after the last
entry time sends one notice. Any later run that day stops in a few seconds.

Results are also saved in the repo: daytrades.json (account and trades) and the
reports/ folder (one file per live day and per backtest).

Paper trading and research only. Not financial advice.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ======================= SETTINGS =======================
START_SAR = 2000.0            # virtual (paper) account size
SAR_PER_USD = 3.75
LOCAL_TZ = "Asia/Riyadh"      # times in messages are shown in your local time
RISK_PER_TRADE = 0.03         # medium risk (low = 0.01, high = 0.06)
MAX_POSITION = 1.0            # at most 100% of the account in one trade (no borrowing)
MAX_TRADES_PER_DAY = 1
MAX_HOLD_MIN = 120            # your 2-hour limit
OR_MINUTES = 15               # opening range length
LAST_ENTRY = dt.time(11, 30)  # no new trades after this (New York time)
REWARD_RISK = 2.0             # target = 2x the distance to the stop
MAX_STOP_PCT = 0.03           # skip trades whose stop is more than 3% away
COMMISSION_PCT = 0.00105      # Sahm: 0.105% per order (buy and sell each)
MIN_FEE_USD = 0.0             # minimum fee per order, if your broker has one
SLIPPAGE = 0.0005             # backtest only: 0.05% worse fill on market orders
PAUSE_BELOW = 0.75            # pause if the account falls below 75% of start
NEAR_LEVEL = 0.75             # warn when price is 75% of the way to the target or stop
TIME_WARNING_MIN = 15         # warn this many minutes before the 2-hour exit
BACKTEST_DAYS = 60
JOB_LIMIT_MIN = 340           # GitHub stops a run after 6 hours; the bot plans to finish within 340 min

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V",
    "MA", "UNH", "XOM", "LLY", "JNJ", "WMT", "PG", "HD", "COST", "ORCL",
    "CRM", "AMD", "NFLX", "ADBE", "PEP", "KO", "MRK", "ABBV", "BAC", "CVX",
    "TMO", "CSCO", "MCD", "ACN", "ABT", "DIS", "WFC", "INTC", "QCOM", "TXN",
    "IBM", "AMGN", "CAT", "GE", "HON", "NOW", "INTU", "UBER", "PFE", "NKE",
    "BA", "GS", "MS", "SBUX", "PLTR", "MU", "PYPL", "SHOP", "COIN", "LOW",
]
# ========================================================

BOOK_FILE = "daytrades.json"
REPORTS_DIR = "reports"
STATS_FILE = "reports/setup-stats.json"  # past success rate, refreshed by every backtest
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
TRADE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
KEY = os.environ.get("ALPACA_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------- helpers ----------------
def now_ny():
    return dt.datetime.now(NY)


def sleep(seconds):
    time.sleep(seconds)


def sleep_until(t):
    while True:
        left = (t - now_ny()).total_seconds()
        if left <= 0:
            return
        sleep(min(left, 30))


def sar(usd):
    return f"{usd * SAR_PER_USD:,.0f} SAR"


LOG = []  # everything this run reported, saved to reports/ at the end


def note(text):
    """Record a line in today's report without sending it to Telegram."""
    print(text)
    now = now_ny()
    LOG.append(f"[{now:%H:%M} New York | {local(now)} your time] {text}")


def write_report(name, lines, mode="a"):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, name)
    with open(path, mode) as f:
        f.write("\n".join(lines) + "\n\n")
    return path


def send(text):
    note(text)
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=30)
    except Exception as e:
        print(f"Telegram failed: {e}")


def api(method, url, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET,
               "Content-Type": "application/json"}
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Alpaca {method} {url.split('?')[0]} -> {e.code}: {detail}")
        except urllib.error.URLError:
            if attempt < 3:
                sleep(5)
                continue
            raise


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_bars(symbols, timeframe, start, end, adjustment="raw"):
    """{symbol: [{t, o, h, l, c, v}, ...]} using the free IEX feed."""
    out = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": iso(start),
              "end": iso(end), "feed": "iex", "limit": 10000, "adjustment": adjustment}
    while True:
        r = api("GET", DATA_URL + "/v2/stocks/bars", params)
        for s, bars in (r.get("bars") or {}).items():
            for b in bars:
                out.setdefault(s, []).append({
                    "t": dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY),
                    "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
        token = r.get("next_page_token")
        if not token:
            return out
        params["page_token"] = token


def calendar(start, end):
    days = api("GET", TRADE_URL + "/v2/calendar",
               {"start": start.isoformat(), "end": end.isoformat()})
    out = []
    for d in days:
        day = dt.date.fromisoformat(d["date"])
        o = dt.datetime.combine(day, dt.time.fromisoformat(d["open"]), NY)
        c = dt.datetime.combine(day, dt.time.fromisoformat(d["close"]), NY)
        out.append((day, o, c))
    return out


def stats_for_day(daily_bars, day):
    """Previous close and 20-day average volume, using only data before `day`."""
    stats = {}
    for s, bars in daily_bars.items():
        past = [b for b in bars if b["t"].date() < day]
        if len(past) >= 10:
            last20 = past[-20:]
            stats[s] = {"prev_close": past[-1]["c"],
                        "avg_vol": sum(b["v"] for b in last20) / len(last20)}
    return stats


# ---------------- strategy (shared by live and backtest) ----------------
def select_candidates(or_bars, stats):
    cands = []
    for s, bars in or_bars.items():
        if s not in stats or len(bars) < 10:
            continue
        hi = max(b["h"] for b in bars)
        lo = min(b["l"] for b in bars)
        last = bars[-1]["c"]
        vol = sum(b["v"] for b in bars)
        if vol <= 0 or stats[s]["avg_vol"] <= 0:
            continue
        vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars) / vol
        rng = (hi - lo) / last
        if last > vwap and last > stats[s]["prev_close"] and 0.003 <= rng <= 0.03:
            cands.append({"symbol": s, "or_high": hi, "or_low": lo, "stop": (hi + lo) / 2,
                          "rvol": vol / stats[s]["avg_vol"]})
    cands.sort(key=lambda c: c["rvol"], reverse=True)
    return cands[:3]


def is_breakout(close, cand):
    rng = cand["or_high"] - cand["or_low"]
    return cand["or_high"] < close <= cand["or_high"] + 0.5 * rng


def plan_trade(cand, entry, balance):
    stop = cand["stop"]
    risk_pct = (entry - stop) / entry
    if risk_pct < 0.001 or risk_pct > MAX_STOP_PCT:
        return None
    notional = min(balance * MAX_POSITION, balance * RISK_PER_TRADE / risk_pct)
    return {"symbol": cand["symbol"], "stop": stop, "notional": round(notional, 2),
            "target": entry + REWARD_RISK * (entry - stop)}


def pnl_text(pnl_usd, invested_usd):
    pct = pnl_usd / invested_usd * 100 if invested_usd else 0
    return f"{pnl_usd * SAR_PER_USD:+.1f} SAR ({pct:+.2f}%)"


def record_text(trades):
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = len(trades) - wins
    net = sum(t["pnl_usd"] for t in trades) * SAR_PER_USD
    return (f"{wins} win{'s' if wins != 1 else ''}, {losses} loss{'es' if losses != 1 else ''}, "
            f"net {net:+.1f} SAR")


def local(t):
    return t.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%H:%M")


def fee(order_value):
    return max(MIN_FEE_USD, order_value * COMMISSION_PCT)


def trade_deadline(entry_t, session_close):
    return min(entry_t + dt.timedelta(minutes=MAX_HOLD_MIN), session_close - dt.timedelta(minutes=5))


def last_entry_time(day, session_close):
    return min(dt.datetime.combine(day, LAST_ENTRY, NY),
               session_close - dt.timedelta(minutes=MAX_HOLD_MIN + 5))


# ---------------- timing on GitHub ----------------
def job_deadline():
    """When this GitHub run must be done (GitHub stops runs after 6 hours)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return dt.datetime.max.replace(tzinfo=NY)  # on your own computer there is no limit
    start = os.environ.get("JOB_START")
    started = dt.datetime.fromtimestamp(float(start), NY) if start else now_ny()
    return started + dt.timedelta(minutes=JOB_LIMIT_MIN)


def work_end_time(day, session_close):
    """Latest moment today's work can finish: last entry + 2-hour hold + a few minutes to sell."""
    return trade_deadline(last_entry_time(day, session_close), session_close) + dt.timedelta(minutes=5)


def start_fresh_run():
    """Ask GitHub to start this workflow again (live mode). Returns True if it accepted."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("Not running on GitHub, so there is no run to restart.")
        return False
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].rsplit("/", 1)[-1] or "assistant.yml"
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": ref, "inputs": {"mode": "live"}}).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
    for attempt in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST", headers=headers),
                                   timeout=30)
            print(f"Fresh run requested on {ref}.")
            return True
        except Exception as e:
            print(f"Restart request failed ({e}).")
            sleep(20)
    return False


def wait_then_restart(work_end):
    """Started too early: wait as long as this run may, then hand over to a fresh run."""
    go = work_end - dt.timedelta(minutes=JOB_LIMIT_MIN)  # from here one run can cover the whole day
    wake = min(go, job_deadline() - dt.timedelta(minutes=10))
    print(f"Started too early for one GitHub run to cover the trading day. "
          f"Waiting until {wake:%H:%M} New York, then starting a fresh run.")
    sleep_until(wake)
    if not start_fresh_run():
        send("⚠️ The bot couldn't restart itself on GitHub this morning, so it may miss today's session.")


def success_text():
    """How often this setup won in the latest backtest (written by `backtest` mode)."""
    try:
        with open(STATS_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        return "Past success rate: not measured yet."
    if not st.get("trades"):
        return "Past success rate: no trades in the last backtest."
    return (f"Past success rate of this setup: {st['win_rate'] * 100:.0f}% "
            f"({st['wins']} of {st['trades']} trades won, {st['from']} to {st['to']}).")


def end_of_day_text(book, today):
    todays = [t for t in book["trades"] if t["date"] == today.isoformat()]
    lines = [f"📊 End of day {today}"]
    if todays:
        pnl = sum(t["pnl_usd"] for t in todays)
        before = book["balance_usd"] - pnl
        lines.append(f"Today: {len(todays)} trade{'s' if len(todays) != 1 else ''}, "
                     f"{pnl * SAR_PER_USD:+.1f} SAR ({pnl / before * 100:+.2f}%)")
    else:
        lines.append("Today: no trade, so no gain or loss.")
    total = book["balance_usd"] - book["start_usd"]
    lines.append(f"Balance: {sar(book['balance_usd'])} | Since start: {total * SAR_PER_USD:+.1f} SAR "
                 f"({total / book['start_usd'] * 100:+.1f}%), {record_text(book['trades'])}")
    return "\n".join(lines)


# ---------------- trade log ----------------
def load_book():
    try:
        with open(BOOK_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        start = round(START_SAR / SAR_PER_USD, 2)
        return {"start_usd": start, "balance_usd": start, "paused": False,
                "last_run": None, "trades": []}


def save_book(book):
    with open(BOOK_FILE, "w") as f:
        json.dump(book, f, indent=2)


def summary(trades, start_usd, end_usd):
    if not trades:
        return f"No trades yet. Balance {sar(end_usd)}."
    wins = [t for t in trades if t["pnl_usd"] > 0]
    exits = {}
    for t in trades:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
    return (f"Trades: {len(trades)} | Winners: {len(wins)} ({len(wins) / len(trades) * 100:.0f}%)\n"
            f"Balance: {sar(start_usd)} → {sar(end_usd)} ({(end_usd / start_usd - 1) * 100:+.1f}%)\n"
            f"Exits: {exits}")


# ---------------- live paper trading ----------------
def wait_fill(order_id, max_wait=60):
    o = {}
    for _ in range(max_wait // 2):
        o = api("GET", f"{TRADE_URL}/v2/orders/{order_id}")
        if o.get("status") == "filled":
            break
        sleep(2)
    qty = float(o.get("filled_qty") or 0)
    price = float(o.get("filled_avg_price") or 0)
    return qty, price, o.get("status")


def latest_price(symbol):
    r = api("GET", DATA_URL + "/v2/stocks/trades/latest", {"symbols": symbol, "feed": "iex"})
    return float(r["trades"][symbol]["p"])


def buy(symbol, notional, price):
    try:
        o = api("POST", TRADE_URL + "/v2/orders", body={
            "symbol": symbol, "notional": f"{notional:.2f}", "side": "buy",
            "type": "market", "time_in_force": "day"})
    except RuntimeError as e:  # fractional not allowed -> whole shares
        qty = int(notional // price)
        if qty < 1:
            print(f"Cannot buy {symbol}: {e}")
            return 0, 0
        o = api("POST", TRADE_URL + "/v2/orders", body={
            "symbol": symbol, "qty": str(qty), "side": "buy",
            "type": "market", "time_in_force": "day"})
    qty, fill, status = wait_fill(o["id"])
    if qty <= 0:
        try:
            api("DELETE", f"{TRADE_URL}/v2/orders/{o['id']}")
        except Exception:
            pass
    return qty, fill


def sell_all(symbol):
    o = api("DELETE", f"{TRADE_URL}/v2/positions/{symbol}")
    qty, fill, _ = wait_fill(o["id"])
    return fill


def run_live():
    now = now_ny()
    today = now.date()
    days = calendar(today, today)
    if not days or days[0][0] != today:
        print("Market closed today.")
        return
    _, sess_open, sess_close = days[0]
    book = load_book()
    if book.get("last_run") == today.isoformat():
        print("Today is already handled.")
        return
    last_entry = last_entry_time(today, sess_close)
    if now >= last_entry:
        book["last_run"] = today.isoformat()
        save_book(book)
        send(f"⚠️ {today}: GitHub started the bot too late ({local(now)} your time), after the last "
             f"entry time ({local(last_entry)}). No trade today.")
        return
    work_end = work_end_time(today, sess_close)
    if work_end > job_deadline():
        wait_then_restart(work_end)
        return

    book["last_run"] = today.isoformat()
    save_book(book)
    note(f"Run started at {now:%H:%M} New York ({local(now)} your time).")
    if book["paused"]:
        send("⏸ Day trader is paused (account fell below the safety limit). Review before restarting.")
        return

    trades_today = trade_one_day(book, today, sess_open, sess_close)
    if trades_today is not None:
        send(end_of_day_text(book, today))
    if book["balance_usd"] < book["start_usd"] * PAUSE_BELOW:
        book["paused"] = True
        send("⏸ Account fell below 75% of the start. Bot paused for review.")
    save_book(book)
    if today.weekday() == 4:
        week = [t for t in book["trades"] if dt.date.fromisoformat(t["date"]) > today - dt.timedelta(days=7)]
        send("📅 Weekly report\nThis week: " + (f"{len(week)} trades, "
             f"{sum(t['pnl_usd'] for t in week) * SAR_PER_USD:+.1f} SAR" if week else "no trades")
             + "\nSince start:\n" + summary(book["trades"], book["start_usd"], book["balance_usd"]))


def trade_one_day(book, today, sess_open, sess_close):
    """Returns the list of trades made today (None if the bot couldn't run)."""
    or_end = sess_open + dt.timedelta(minutes=OR_MINUTES)
    last_entry = last_entry_time(today, sess_close)
    sleep_until(or_end + dt.timedelta(seconds=10))
    if now_ny() >= last_entry:
        send("⚠️ Day trader started too late today (GitHub delay). No trades.")
        return None

    daily = get_bars(WATCHLIST, "1Day", sess_open - dt.timedelta(days=45),
                     sess_open - dt.timedelta(hours=10), adjustment="split")
    stats = stats_for_day(daily, today)
    or_bars = get_bars(WATCHLIST, "1Min", sess_open, or_end)
    or_bars = {s: [b for b in bars if b["t"] < or_end] for s, bars in or_bars.items()}
    cands = select_candidates(or_bars, stats)
    if not cands:
        send(f"📊 {today}: no stocks passed the filters.")
        return []
    send(f"👀 {today} — watching for breakouts until {local(last_entry)} (your time):\n" + "\n".join(
        f"{c['symbol']}: buy above ${c['or_high']:.2f} (volume {c['rvol'] * 100:.0f}% of a normal day already)"
        for c in cands) + f"\nThe first one to break out is today's pick.\n{success_text()}")

    active = {c["symbol"]: c for c in cands}
    trades_today = []
    while active and len(trades_today) < MAX_TRADES_PER_DAY and now_ny() < last_entry:
        nxt = now_ny().replace(second=5, microsecond=0) + dt.timedelta(minutes=1)
        sleep_until(nxt)
        now = now_ny()
        bars = get_bars(list(active), "1Min", or_end, now)
        signal = None
        for s, c in active.items():
            done = [b for b in bars.get(s, []) if b["t"] >= or_end and b["t"] + dt.timedelta(minutes=1) <= now]
            if done and is_breakout(done[-1]["c"], c):
                signal = c
                break
        if not signal:
            continue

        s = signal["symbol"]
        price = latest_price(s)
        plan = plan_trade(signal, price, book["balance_usd"])
        if not plan:
            active.pop(s)
            continue
        qty, entry = buy(s, plan["notional"], price)
        if qty <= 0:
            active.pop(s)
            continue
        entry_t = now_ny()
        stop = plan["stop"]
        target = entry + REWARD_RISK * (entry - stop)
        deadline = trade_deadline(entry_t, sess_close)
        send(f"✅ BOUGHT {s}: {qty:.4f} shares at ${entry:.2f} (${qty * entry:.2f} ≈ {sar(qty * entry)})\n"
             f"Stop ${stop:.2f} | Target ${target:.2f} | Sell by {local(deadline)} (your time) at the latest\n"
             f"{success_text()}")

        invested = qty * entry
        near_target = entry + NEAR_LEVEL * (target - entry)
        near_stop = entry - NEAR_LEVEL * (entry - stop)
        warned = set()
        reason = None
        try:
            while reason is None:
                sleep(20)
                p = latest_price(s)
                if p <= stop:
                    reason = "stop"
                elif p >= target:
                    reason = "target"
                elif now_ny() >= deadline:
                    reason = "time"
                else:
                    open_pnl = qty * (p - entry) - fee(invested) - fee(qty * p)
                    if p >= near_target and "target" not in warned:
                        warned.add("target")
                        send(f"🔥 {s} is close to the TARGET: now ${p:.2f}, target ${target:.2f}\n"
                             f"Profit right now: {pnl_text(open_pnl, invested)}")
                    elif p <= near_stop and "stop" not in warned:
                        warned.add("stop")
                        send(f"⚠️ {s} is close to the STOP: now ${p:.2f}, stop ${stop:.2f}\n"
                             f"Loss right now: {pnl_text(open_pnl, invested)}")
                    if (now_ny() >= deadline - dt.timedelta(minutes=TIME_WARNING_MIN)
                            and "time" not in warned):
                        warned.add("time")
                        send(f"⏳ {s}: {TIME_WARNING_MIN} minutes left before the 2-hour exit ({local(deadline)} your time).\n"
                             f"Now ${p:.2f} | {'Profit' if open_pnl > 0 else 'Loss'} right now: "
                             f"{pnl_text(open_pnl, invested)}")
        finally:
            exit_price = sell_all(s)
        if exit_price <= 0:
            exit_price = latest_price(s)
        pnl = qty * (exit_price - entry) - fee(qty * entry) - fee(qty * exit_price)
        book["balance_usd"] = round(book["balance_usd"] + pnl, 2)
        trade = {"date": today.isoformat(), "symbol": s, "entry": round(entry, 4),
                 "exit": round(exit_price, 4), "qty": qty, "exit_reason": reason,
                 "minutes": round((now_ny() - entry_t).total_seconds() / 60),
                 "pnl_usd": round(pnl, 2), "balance_usd": book["balance_usd"]}
        book["trades"].append(trade)
        trades_today.append(trade)
        save_book(book)
        icon = {"target": "🎯", "stop": "🛑", "time": "⏰"}[reason]
        label = {"target": "hit target", "stop": "hit stop", "time": "2 hours passed"}[reason]
        verdict = "✅ WIN" if pnl > 0 else "❌ LOSS"
        send(f"{icon} SOLD {s} at ${exit_price:.2f} ({label}) after {trade['minutes']} min\n"
             f"{verdict}: {pnl_text(pnl, invested)}\n"
             f"Balance: {sar(book['balance_usd'])} | Since start: {record_text(book['trades'])}")
        active.pop(s)
    return trades_today


# ---------------- backtest ----------------
def simulate_day(day_bars, stats, balance, day, sess_open, sess_close):
    or_end = sess_open + dt.timedelta(minutes=OR_MINUTES)
    or_bars = {s: [b for b in bars if b["t"] < or_end] for s, bars in day_bars.items()}
    cands = select_candidates(or_bars, stats)
    last_entry = last_entry_time(day, sess_close)
    trades = []
    busy_until = or_end
    active = {c["symbol"]: c for c in cands}
    while active and len(trades) < MAX_TRADES_PER_DAY:
        # earliest breakout candle after busy_until
        best = None
        for s, c in active.items():
            for i, b in enumerate(day_bars[s]):
                if b["t"] < busy_until or b["t"] >= last_entry:
                    continue
                if is_breakout(b["c"], c):
                    if best is None or b["t"] < best[2]["t"]:
                        best = (s, i, b)
                    break
        if best is None:
            break
        s, i, sig = best
        bars = day_bars[s]
        if i + 1 >= len(bars):
            active.pop(s)
            continue
        eb = bars[i + 1]
        entry = eb["o"] * (1 + SLIPPAGE)
        plan = plan_trade(active[s], entry, balance)
        if not plan:
            active.pop(s)
            continue
        stop = plan["stop"]
        target = entry + REWARD_RISK * (entry - stop)
        deadline = trade_deadline(eb["t"], sess_close)
        exit_price, reason, exit_t = None, None, None
        for b in bars[i + 1:]:
            if b["t"] >= deadline:
                exit_price, reason, exit_t = b["o"] * (1 - SLIPPAGE), "time", b["t"]
                break
            if b["l"] <= stop:
                exit_price, reason, exit_t = min(stop, b["o"]) * (1 - SLIPPAGE), "stop", b["t"]
                break
            if b["h"] >= target:
                exit_price, reason, exit_t = max(target, b["o"]) * (1 - SLIPPAGE), "target", b["t"]
                break
        if exit_price is None:
            last = [b for b in bars if b["t"] < deadline][-1]
            exit_price, reason, exit_t = last["c"] * (1 - SLIPPAGE), "time", last["t"]
        qty = plan["notional"] / entry
        pnl = qty * (exit_price - entry) - fee(qty * entry) - fee(qty * exit_price)
        trades.append({"date": day.isoformat(), "symbol": s, "entry": round(entry, 2),
                       "exit": round(exit_price, 2), "exit_reason": reason,
                       "pnl_usd": round(pnl, 2), "pct_of_account": pnl / balance})
        balance += pnl
        busy_until = exit_t + dt.timedelta(minutes=1)
        active.pop(s)
    return trades, balance, len(cands)


def run_backtest():
    today = now_ny().date()
    days = calendar(today - dt.timedelta(days=BACKTEST_DAYS * 2), today - dt.timedelta(days=1))
    days = days[-BACKTEST_DAYS:]
    first = days[0][1]
    daily = get_bars(WATCHLIST + ["SPY"], "1Day", first - dt.timedelta(days=45),
                     days[-1][2], adjustment="split")
    start = balance = round(START_SAR / SAR_PER_USD, 2)
    trades, peak, max_dd, quiet, no_cands, day_lines = [], balance, 0.0, 0, 0, []
    for day, sess_open, sess_close in days:
        stats = stats_for_day(daily, day)
        end = min(sess_open + dt.timedelta(hours=4, minutes=30), sess_close)
        bars = get_bars(WATCHLIST, "1Min", sess_open, end)
        day_trades, balance, n_cands = simulate_day(bars, stats, balance, day, sess_open, sess_close)
        trades += day_trades
        quiet += 0 if day_trades else 1
        no_cands += 0 if n_cands else 1
        peak = max(peak, balance)
        max_dd = max(max_dd, 1 - balance / peak)
        what = ", ".join(f"{t['symbol']} {t['exit_reason']} {t['pnl_usd'] * SAR_PER_USD:+.1f} SAR"
                         for t in day_trades) or "no trade"
        day_lines.append(f"{day}: {n_cands} stock(s) passed the filters, {what}. Balance {sar(balance)}")
        print(day_lines[-1])

    spy = [b for b in daily.get("SPY", []) if b["t"].date() <= days[-1][0]]
    spy_before = [b for b in spy if b["t"].date() < days[0][0]]
    spy_ret = (spy[-1]["c"] / spy_before[-1]["c"] - 1) * 100 if spy and spy_before else float("nan")
    lines = [f"🧪 Day-trading backtest: last {len(days)} trading days "
             f"({days[0][0]} to {days[-1][0]}), medium risk, max 2-hour hold", ""]
    lines.append(summary(trades, start, balance))
    if trades:
        pcts = [t["pct_of_account"] * 100 for t in trades]
        lines.append(f"Avg trade: {sum(pcts) / len(pcts):+.2f}% of account | "
                     f"Best {max(pcts):+.1f}% | Worst {min(pcts):+.1f}%")
    lines += [f"Days with no trade: {quiet} (no stock passed the filters on {no_cands})",
              f"Biggest drop from a peak: {max_dd * 100:.1f}%",
              f"SPY buy-and-hold same period: {spy_ret:+.1f}%",
              f"Fees {COMMISSION_PCT * 100:.3f}% per order (Sahm), slippage {SLIPPAGE * 100:.2f}%. "
              "IEX data only, so volume is partial."]
    report = lines + ["", "Every trade (date, stock, entry -> exit, why it closed, result):"]
    report += [f"{t['date']} {t['symbol']:<5} ${t['entry']:.2f} -> ${t['exit']:.2f} {t['exit_reason']:<6} "
               f"{t['pnl_usd'] * SAR_PER_USD:+.1f} SAR ({t['pct_of_account'] * 100:+.2f}% of account)"
               for t in trades] or ["(none)"]
    report += ["", "Day by day:"] + day_lines
    path = write_report(f"backtest-{today}.txt", report, mode="w")
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    with open(STATS_FILE, "w") as f:
        json.dump({"trades": len(trades), "wins": wins, "win_rate": wins / len(trades) if trades else 0.0,
                   "from": days[0][0].isoformat(), "to": days[-1][0].isoformat(), "updated": today.isoformat()},
                  f, indent=2)
    print(f"Saved {path}")
    send("\n".join(lines))


def run_notify():
    text = os.environ.get("MESSAGE", "").strip()
    if text:
        send(text)
    else:
        print("No message to send.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    try:
        {"live": run_live, "backtest": run_backtest, "notify": run_notify}[mode]()
    except Exception as e:
        send(f"⚠️ Day trader error ({mode}): {e}")
        raise
    finally:
        if mode == "live" and LOG:
            write_report(f"live-{now_ny().date()}.txt", LOG)
