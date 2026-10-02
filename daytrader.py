"""
Day Trader Bot v2 (PAPER trading)
Prepared by: Eng. Mohammed T. Basaqr

Strategy: Opening Range Breakout, long only, every trade closed within 2 hours.
  1. 9:30-9:45 New York time: record each stock's first-15-minute high and low.
  2. Watch the 5 busiest stocks that are up on the day and above their average price (VWAP).
  3. Buy when a 1-minute candle closes just above its 15-minute high, any time until 1:55 PM,
     but only while the overall market (S&P 500 / SPY) is not down more than 1% on the day.
  4. Stop = middle of the 15-minute range. Target = 2x the risk. Exit anyway after 2 hours.
  5. Up to 3 trades a day, each with a third of the money, so they can run at the same time.
     Never borrows money. Each pick shows the market, the stock's latest news and the
     setup's past success rate.

Modes:
  python daytrader.py live            -> trades one day on the Alpaca PAPER account
  python daytrader.py backtest        -> replays the rules on the last 60 trading days
  python daytrader.py notify          -> sends the text in MESSAGE to Telegram (updates from Claude)
  python daytrader.py telegram_check  -> checks the Telegram settings

Timing on GitHub: scheduled runs often start hours late, so the workflow has several
wake-up calls, and one run can only last 6 hours. A run that starts long before the open
waits, then starts a fresh run shortly before 9:30 AM. A run that reaches its time limit
with work left saves the open trades and hands over to a fresh run, which checks what
happened in between and carries on. A run that starts after the last entry time sends one
notice. Any later run that day stops in a few seconds.

Results are also saved in the repo: daytrades.json (account and trades) and the
reports/ folder (one file per live day and per backtest).

Paper trading and research only. Not financial advice.
"""
import datetime as dt
import json
import re
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
RISK_PER_TRADE = 0.03         # never risk more than 3% of the account on one trade
MAX_TRADES_PER_DAY = 3        # up to 3 trades a day ...
SLOTS = 3                     # ... each with a third of the day's money, so they can overlap
CANDIDATES = 5                # watch the 5 busiest stocks that pass the morning filters
MAX_HOLD_MIN = 120            # your 2-hour limit
OR_MINUTES = 15               # opening range length
LAST_ENTRY = dt.time(13, 55)  # last new trade at 1:55 PM New York, so every exit is before the close
MARKET_FLOOR = -0.01          # no new buys while the S&P 500 (SPY) is down more than 1% today
NEWS_HOURS = 24               # headlines shown with each pick
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
START_BEFORE_OPEN_MIN = 10    # the trading run should be up about 10 minutes before the open

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
RAW_TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
RAW_TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_TOKEN = RAW_TG_TOKEN.strip()  # stray spaces or line breaks pasted into a secret would break sending
TG_CHAT = RAW_TG_CHAT.strip()


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
    LOG.append(f"[{clock(now)} New York | {local(now)} your time] {text}")


def write_report(name, lines, mode="a"):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, name)
    with open(path, mode) as f:
        f.write("\n".join(lines) + "\n\n")
    return path


def hide_secrets(text):
    for secret in {RAW_TG_TOKEN, TG_TOKEN, RAW_TG_CHAT, TG_CHAT}:
        if secret:
            text = text.replace(secret, "<hidden>")
    return text


def telegram(method, data=None):
    """Call the Telegram API; returns Telegram's answer, or {"ok": False, "description": why}."""
    body = urllib.parse.urlencode(data).encode() if data else None
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=body, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": hide_secrets(str(e))}


def send(text):
    note(text)
    if not TG_TOKEN or not TG_CHAT:
        if os.environ.get("GITHUB_ACTIONS"):
            note("(Telegram not sent: the TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secret is missing.)")
        return
    answer = telegram("sendMessage", {"chat_id": TG_CHAT, "text": text[:4000]})
    if not answer.get("ok"):
        note(f"(Telegram failed: {hide_secrets(str(answer.get('description')))})")


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
def select_candidates(or_bars, stats, n=CANDIDATES):
    cands = []
    for s, bars in or_bars.items():
        if s not in WATCHLIST or s not in stats or len(bars) < 10:
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
    return cands[:n]


def is_breakout(close, cand):
    rng = cand["or_high"] - cand["or_low"]
    return cand["or_high"] < close <= cand["or_high"] + 0.5 * rng


def plan_trade(cand, entry, slot_usd, balance):
    """Size one trade: a slot of the day's money, never risking more than RISK_PER_TRADE."""
    stop = cand["stop"]
    risk_pct = (entry - stop) / entry
    if risk_pct < 0.001 or risk_pct > MAX_STOP_PCT:
        return None
    notional = min(slot_usd, balance * RISK_PER_TRADE / risk_pct)
    return {"symbol": cand["symbol"], "stop": stop, "notional": round(notional, 2),
            "target": entry + REWARD_RISK * (entry - stop)}


def market_ok(change):
    """change = S&P 500 (SPY) move today as a fraction; unknown counts as OK."""
    return change is None or change > MARKET_FLOOR


def market_text(change):
    if change is None:
        return "Market (S&P 500): not available right now."
    if market_ok(change):
        return f"Market (S&P 500): {change * 100:+.2f}% today ✅"
    return f"Market (S&P 500): {change * 100:+.2f}% today ⛔ no new buys while it's down more than 1%"


def headlines(symbol, now, limit=2):
    """Latest headlines about a stock (Alpaca news). None if they couldn't be loaded."""
    try:
        r = api("GET", DATA_URL + "/v1beta1/news", {
            "symbols": symbol, "start": iso(now - dt.timedelta(hours=NEWS_HOURS)), "end": iso(now),
            "limit": limit, "sort": "desc"})
        return [n["headline"].strip() for n in r.get("news", []) if n.get("headline")][:limit]
    except Exception as e:
        print(f"News for {symbol} unavailable: {e}")
        return None


def news_text(items):
    if items is None:
        return "📰 News: couldn't load it right now."
    if not items:
        return f"📰 No news about it in the last {NEWS_HOURS} hours."
    return f"📰 News (last {NEWS_HOURS} hours):\n" + "\n".join(f"• {h}" for h in items)


def day_label(day):
    return f"{day:%a %b} {day.day}"


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
    return clock(t.astimezone(ZoneInfo(LOCAL_TZ)))


def clock(t):
    """12-hour time, e.g. 8:55 PM."""
    return t.strftime("%I:%M %p").lstrip("0")


def fee(order_value):
    return max(MIN_FEE_USD, order_value * COMMISSION_PCT)


def trade_deadline(entry_t, session_close):
    return min(entry_t + dt.timedelta(minutes=MAX_HOLD_MIN), session_close - dt.timedelta(minutes=5))


def todays_cutoff():
    """LAST_ENTRY, unless a run was started with a one-off later cutoff (LAST_ENTRY_NY="HH:MM")."""
    value = os.environ.get("LAST_ENTRY_NY", "").strip()
    if not value:
        return LAST_ENTRY
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        print(f"Ignoring LAST_ENTRY_NY={value!r} (use HH:MM, New York time).")
        return LAST_ENTRY


def last_entry_time(day, session_close):
    return min(dt.datetime.combine(day, todays_cutoff(), NY),
               session_close - dt.timedelta(minutes=MAX_HOLD_MIN + 5))


# ---------------- timing on GitHub ----------------
def job_deadline():
    """When this GitHub run must be done (GitHub stops runs after 6 hours)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return dt.datetime.max.replace(tzinfo=NY)  # on your own computer there is no limit
    start = os.environ.get("JOB_START")
    started = dt.datetime.fromtimestamp(float(start), NY) if start else now_ny()
    return started + dt.timedelta(minutes=JOB_LIMIT_MIN)


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


def today_trades(book, today):
    return [t for t in book["trades"] if t["date"] == today.isoformat()]


def end_of_day_text(book, today):
    todays = today_trades(book, today)
    lines = [f"📊 End of day, {day_label(today)}"]
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


def latest_prices(symbols):
    r = api("GET", DATA_URL + "/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": "iex"})
    return {s: float(t["p"]) for s, t in (r.get("trades") or {}).items()}


def latest_price(symbol):
    return latest_prices([symbol])[symbol]


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


def spy_change(stats):
    """S&P 500 (SPY) move today right now, as a fraction (None if unknown)."""
    prev = stats.get("SPY", {}).get("prev_close")
    try:
        return latest_price("SPY") / prev - 1 if prev else None
    except Exception as e:
        print(f"Market price unavailable: {e}")
        return None


def run_live():
    now = now_ny()
    today = now.date()
    days = calendar(today, today)
    if not days or days[0][0] != today:
        print("Market closed today.")
        return
    _, sess_open, sess_close = days[0]
    book = load_book()
    book.setdefault("open", [])
    if (book.get("day") or {}).get("date") != today.isoformat():
        book["day"] = {"date": today.isoformat(), "start_usd": book["balance_usd"], "done": False}
    day = book["day"]
    if todays_cutoff() != LAST_ENTRY and day["done"] and not book["open"]:
        day.update(done=False, announced=False)  # a one-off extra session started by hand
    if day["done"] and not book["open"]:
        print("Today is already handled.")
        return
    last_entry = last_entry_time(today, sess_close)
    if now >= last_entry and not book["open"]:
        book["last_run"] = today.isoformat()
        if day.get("announced"):  # the day had started; a hand-over landed after the last entry time
            finish_day(book, today)
            return
        day["done"] = True
        save_book(book)
        send(f"⚠️ {day_label(today)}: GitHub started the bot too late ({local(now)} your time), after the "
             f"last entry time ({local(last_entry)}). No trade today.")
        return
    go = sess_open - dt.timedelta(minutes=START_BEFORE_OPEN_MIN)
    if now < go and os.environ.get("GITHUB_ACTIONS"):
        # Too early: wait, then let a fresh run (with a fresh 6-hour limit) do the day.
        wake = min(go, job_deadline() - dt.timedelta(minutes=10))
        print(f"Too early. Waiting until {clock(wake)} New York, then starting a fresh run.")
        sleep_until(wake)
        if not start_fresh_run():
            send("⚠️ The bot couldn't restart itself on GitHub this morning, so it may miss today's session.")
        return
    now = max(now, now_ny())

    book["last_run"] = today.isoformat()
    save_book(book)
    note(f"Run started at {clock(now)} New York ({local(now)} your time).")
    if book["paused"]:
        day["done"] = True
        save_book(book)
        send("⏸ Day trader is paused (account fell below the safety limit). Review before restarting.")
        return
    try:
        finished = work_day(book, today, sess_open, sess_close)
    except Exception as e:
        close_everything(book, today, f"something went wrong ({e})")
        raise
    if finished:
        finish_day(book, today)


def work_day(book, today, sess_open, sess_close):
    """Runs the trading day. True when the day is finished, False after handing over to a fresh run."""
    day = book["day"]
    or_end = sess_open + dt.timedelta(minutes=OR_MINUTES)
    last_entry = last_entry_time(today, sess_close)
    hand_over_at = job_deadline() - dt.timedelta(minutes=10)

    sleep_until(min(or_end + dt.timedelta(seconds=10), hand_over_at))
    if now_ny() >= hand_over_at:
        return hand_over(book, today)

    daily = get_bars(WATCHLIST + ["SPY"], "1Day", sess_open - dt.timedelta(days=45),
                     sess_open - dt.timedelta(hours=10), adjustment="split")
    stats = stats_for_day(daily, today)
    or_bars = get_bars(WATCHLIST, "1Min", sess_open, or_end)
    or_bars = {s: [b for b in bars if b["t"] < or_end] for s, bars in or_bars.items()}
    cands = select_candidates(or_bars, stats)
    taken = {t["symbol"] for t in today_trades(book, today)} | {p["symbol"] for p in book["open"]}
    active = {c["symbol"]: c for c in cands if c["symbol"] not in taken}
    rank = {c["symbol"]: i for i, c in enumerate(cands)}

    if not day.get("announced"):
        announce(book, today, cands, last_entry, spy_change(stats))
        day["announced"] = True
        save_book(book)
    now = now_ny()
    if day.get("scanning"):  # resuming after a hand-over: don't act on old signals
        scan_from = max(or_end, now.replace(second=0, microsecond=0) - dt.timedelta(minutes=1))
    else:
        scan_from = or_end
    day["scanning"] = True
    check_gap(book, today)

    while True:
        now = now_ny()
        room = MAX_TRADES_PER_DAY - len(today_trades(book, today)) - len(book["open"])
        can_enter = now < last_entry and room > 0 and bool(active)
        if not book["open"] and not can_enter:
            return True
        if now >= hand_over_at:
            return hand_over(book, today)
        if book["open"]:
            manage_positions(book, today)
        if can_enter and now >= scan_from + dt.timedelta(minutes=1, seconds=5):
            scan_from = scan_for_entries(book, today, sess_close, stats, active, rank, scan_from, last_entry)
        sleep(20)


def announce(book, today, cands, last_entry, change):
    if not cands:
        send(f"📊 {day_label(today)}: no stocks passed the morning filters, so no trades today.\n"
             f"{market_text(change)}")
        return
    now = now_ny()
    lines = [f"👀 {day_label(today)} picks: watching for breakouts until {local(last_entry)} (your time)",
             market_text(change)]
    marked = False
    for i, c in enumerate(cands, 1):
        news = headlines(c["symbol"], now, limit=1)
        marked |= bool(news)
        lines.append(f"{i}. {c['symbol']}: buy above ${c['or_high']:.2f} "
                     f"(volume {c['rvol'] * 100:.0f}% of a normal day already){' 📰' if news else ''}")
    lines.append(f"Up to {MAX_TRADES_PER_DAY} trades today, about {sar(book['day']['start_usd'] / SLOTS)} each. "
                 "The first ones to break out get bought.")
    if marked:
        lines.append(f"📰 = news about the stock in the last {NEWS_HOURS} hours")
    lines.append(success_text())
    send("\n".join(lines))


def scan_for_entries(book, today, sess_close, stats, active, rank, scan_from, last_entry):
    """Look at the 1-minute candles finished since scan_from; buy breakouts while there is room.
    Returns the time to scan from next."""
    now = now_ny()
    bars = get_bars(list(active) + ["SPY"], "1Min", scan_from, now)
    done_by = now - dt.timedelta(minutes=1)
    spy_prev = stats.get("SPY", {}).get("prev_close")
    spy = [b for b in bars.get("SPY", []) if b["t"] <= done_by]

    def change_at(t):
        closes = [b["c"] for b in spy if b["t"] <= t]
        return closes[-1] / spy_prev - 1 if closes and spy_prev else None

    signals, latest = [], scan_from - dt.timedelta(minutes=1)
    for s, c in active.items():
        for b in bars.get(s, []):
            if b["t"] < scan_from or b["t"] > done_by or b["t"] >= last_entry:
                continue
            latest = max(latest, b["t"])
            if not is_breakout(b["c"], c):
                continue
            change = change_at(b["t"])
            if market_ok(change):
                signals.append((b["t"], rank[s], s, change))
                break
            skipped = book["day"].setdefault("skipped", [])
            if s not in skipped:
                skipped.append(s)
                send(f"⏸ {s} broke out at {local(b['t'] + dt.timedelta(minutes=1))} (your time), but the "
                     f"S&P 500 is down {abs(change) * 100:.2f}% today, so no buy for now.")
    for t, _, s, change in sorted(signals):
        if MAX_TRADES_PER_DAY - len(today_trades(book, today)) - len(book["open"]) <= 0:
            break
        open_position(book, today, sess_close, active.pop(s), change)
    return max(scan_from, latest + dt.timedelta(minutes=1))


def open_position(book, today, sess_close, cand, change):
    s = cand["symbol"]
    price = latest_price(s)
    plan = plan_trade(cand, price, book["day"]["start_usd"] / SLOTS, book["day"]["start_usd"])
    if not plan:
        note(f"Skipped {s}: its stop would be too close or too far.")
        return
    qty, entry = buy(s, plan["notional"], price)
    if qty <= 0:
        note(f"Skipped {s}: the buy order didn't fill.")
        return
    now = now_ny()
    stop = plan["stop"]
    target = entry + REWARD_RISK * (entry - stop)
    deadline = trade_deadline(now, sess_close)
    book["open"].append({"symbol": s, "qty": qty, "entry": entry, "stop": stop, "target": target,
                         "entry_time": now.isoformat(), "deadline": deadline.isoformat(), "warned": []})
    save_book(book)
    number = len(today_trades(book, today)) + len(book["open"])
    send(f"✅ BOUGHT {s} (trade {number} of {MAX_TRADES_PER_DAY} today): {qty:.4f} shares at ${entry:.2f} "
         f"(≈ {sar(qty * entry)})\n"
         f"Stop ${stop:.2f} | Target ${target:.2f} | Sell by {local(deadline)} (your time) at the latest\n"
         f"{market_text(change)}\n{news_text(headlines(s, now))}\n{success_text()}")


def manage_positions(book, today):
    prices = latest_prices([p["symbol"] for p in book["open"]])
    now = now_ny()
    for p in list(book["open"]):
        price = prices.get(p["symbol"])
        if price is None:
            continue
        s, qty, entry, stop, target = p["symbol"], p["qty"], p["entry"], p["stop"], p["target"]
        deadline = dt.datetime.fromisoformat(p["deadline"])
        reason = ("stop" if price <= stop else "target" if price >= target
                  else "time" if now >= deadline else None)
        if reason:
            close_position(book, today, p, reason)
            continue
        invested = qty * entry
        open_pnl = qty * (price - entry) - fee(invested) - fee(qty * price)
        if price >= entry + NEAR_LEVEL * (target - entry) and "target" not in p["warned"]:
            p["warned"].append("target")
            send(f"🔥 {s} is close to the TARGET: now ${price:.2f}, target ${target:.2f}\n"
                 f"Profit right now: {pnl_text(open_pnl, invested)}")
        elif price <= entry - NEAR_LEVEL * (entry - stop) and "stop" not in p["warned"]:
            p["warned"].append("stop")
            send(f"⚠️ {s} is close to the STOP: now ${price:.2f}, stop ${stop:.2f}\n"
                 f"Loss right now: {pnl_text(open_pnl, invested)}")
        if now >= deadline - dt.timedelta(minutes=TIME_WARNING_MIN) and "time" not in p["warned"]:
            p["warned"].append("time")
            send(f"⏳ {s}: {TIME_WARNING_MIN} minutes left before the 2-hour exit ({local(deadline)} your time).\n"
                 f"Now ${price:.2f} | {'Profit' if open_pnl > 0 else 'Loss'} right now: "
                 f"{pnl_text(open_pnl, invested)}")
    save_book(book)


def close_position(book, today, p, reason):
    s = p["symbol"]
    try:
        exit_price = sell_all(s)
    except Exception as e:
        note(f"Selling {s} reported a problem: {e}")
        exit_price = 0
    if exit_price <= 0:
        exit_price = latest_price(s)
    qty, entry = p["qty"], p["entry"]
    invested = qty * entry
    pnl = qty * (exit_price - entry) - fee(invested) - fee(qty * exit_price)
    book["balance_usd"] = round(book["balance_usd"] + pnl, 2)
    minutes = round((now_ny() - dt.datetime.fromisoformat(p["entry_time"])).total_seconds() / 60)
    book["open"].remove(p)
    book["trades"].append({"date": today.isoformat(), "symbol": s, "entry": round(entry, 4),
                           "exit": round(exit_price, 4), "qty": qty, "exit_reason": reason,
                           "minutes": minutes, "pnl_usd": round(pnl, 2), "balance_usd": book["balance_usd"]})
    save_book(book)
    number = len(today_trades(book, today))
    icon = {"target": "🎯", "stop": "🛑", "time": "⏰"}.get(reason, "⚠️")
    label = {"target": "hit target", "stop": "hit stop", "time": "2 hours passed"}.get(reason, "closed for safety")
    send(f"{icon} SOLD {s} at ${exit_price:.2f} ({label}) after {minutes} min, trade {number} of "
         f"{MAX_TRADES_PER_DAY} today\n"
         f"{'✅ WIN' if pnl > 0 else '❌ LOSS'}: {pnl_text(pnl, invested)}\n"
         f"Balance: {sar(book['balance_usd'])} | Since start: {record_text(book['trades'])}")


def check_gap(book, today):
    """After a hand-over, see whether a stop or target was reached while no run was watching."""
    now = now_ny()
    for p in list(book["open"]):
        since = dt.datetime.fromisoformat(p.get("checked") or p["entry_time"])
        bars = get_bars([p["symbol"]], "1Min", since, now).get(p["symbol"], [])
        for b in bars:
            if b["l"] <= p["stop"]:
                close_position(book, today, p, "stop")
                break
            if b["h"] >= p["target"]:
                close_position(book, today, p, "target")
                break


def hand_over(book, today):
    """Close to GitHub's 6-hour limit: save the open trades and start a fresh run to carry on."""
    now = now_ny()
    for p in book["open"]:
        p["checked"] = now.isoformat()
    save_book(book)
    note(f"Handing over to a fresh run ({len(book['open'])} open trade(s)).")
    if start_fresh_run():
        return False
    close_everything(book, today, "the bot couldn't hand over to a fresh run")
    return True


def close_everything(book, today, why):
    """Safety net: never leave a trade open beyond its 2 hours."""
    if not book["open"]:
        return
    send(f"⚠️ {why[:300]}. To stay safe, closing the open trades now.")
    for p in list(book["open"]):
        try:
            close_position(book, today, p, "safety")
        except Exception as e:
            note(f"Could not close {p['symbol']}: {e}")
    book["day"]["done"] = True
    save_book(book)


def finish_day(book, today):
    book["day"]["done"] = True
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


# ---------------- backtest ----------------
def simulate_day(day_bars, stats, balance, day, sess_open, sess_close):
    """Replays one day with the live rules: each watched stock's first breakout while the market
    is OK, earliest first, up to MAX_TRADES_PER_DAY trades with a slot of the money each."""
    or_end = sess_open + dt.timedelta(minutes=OR_MINUTES)
    or_bars = {s: [b for b in bars if b["t"] < or_end] for s, bars in day_bars.items() if s in WATCHLIST}
    cands = select_candidates(or_bars, stats)
    last_entry = last_entry_time(day, sess_close)
    spy_prev = stats.get("SPY", {}).get("prev_close")
    spy = day_bars.get("SPY", [])

    def change_at(t):
        closes = [b["c"] for b in spy if b["t"] <= t]
        return closes[-1] / spy_prev - 1 if closes and spy_prev else None

    signals = []
    for rank, c in enumerate(cands):
        for i, b in enumerate(day_bars[c["symbol"]]):
            if b["t"] < or_end or b["t"] >= last_entry:
                continue
            if is_breakout(b["c"], c) and market_ok(change_at(b["t"])):
                signals.append((b["t"], rank, c, i))
                break
    signals.sort(key=lambda x: (x[0], x[1]))

    slot, trades, day_pnl = balance / SLOTS, [], 0.0
    for _, _, c, i in signals:
        if len(trades) >= MAX_TRADES_PER_DAY:
            break
        s, bars = c["symbol"], day_bars[c["symbol"]]
        if i + 1 >= len(bars):
            continue
        eb = bars[i + 1]
        entry = eb["o"] * (1 + SLIPPAGE)
        plan = plan_trade(c, entry, slot, balance)
        if not plan:
            continue
        stop = plan["stop"]
        target = entry + REWARD_RISK * (entry - stop)
        deadline = trade_deadline(eb["t"], sess_close)
        exit_price, reason = None, None
        for b in bars[i + 1:]:
            if b["t"] >= deadline:
                exit_price, reason = b["o"] * (1 - SLIPPAGE), "time"
                break
            if b["l"] <= stop:
                exit_price, reason = min(stop, b["o"]) * (1 - SLIPPAGE), "stop"
                break
            if b["h"] >= target:
                exit_price, reason = max(target, b["o"]) * (1 - SLIPPAGE), "target"
                break
        if exit_price is None:
            last = [b for b in bars if b["t"] < deadline][-1]
            exit_price, reason = last["c"] * (1 - SLIPPAGE), "time"
        qty = plan["notional"] / entry
        pnl = qty * (exit_price - entry) - fee(qty * entry) - fee(qty * exit_price)
        trades.append({"date": day.isoformat(), "symbol": s, "entry": round(entry, 2),
                       "exit": round(exit_price, 2), "exit_reason": reason,
                       "pnl_usd": round(pnl, 2), "pct_of_account": pnl / balance})
        day_pnl += pnl
    return trades, balance + day_pnl, len(cands)


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
        bars = get_bars(WATCHLIST + ["SPY"], "1Min", sess_open, sess_close)
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
    lines = [f"🧪 Day-trading backtest: last {len(days)} trading days ({days[0][0]} to {days[-1][0]})",
             f"Rules: up to {MAX_TRADES_PER_DAY} trades a day with a third of the money each, entries until "
             f"{clock(dt.datetime.combine(days[0][0], LAST_ENTRY))} New York, no buys when the S&P 500 is down "
             f"more than {abs(MARKET_FLOOR) * 100:g}%, max 2-hour hold", ""]
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


def run_telegram_check():
    """Checks the Telegram secrets without revealing them. Result: reports/telegram-check.txt."""
    def shape(raw, pattern):
        if not raw:
            return "MISSING"
        notes = ["set", "format OK" if re.fullmatch(pattern, raw.strip()) else "format WRONG"]
        if raw != raw.strip():
            notes.append("had extra spaces or line breaks (now ignored)")
        return ", ".join(notes)

    lines = [f"Telegram check, {now_ny():%Y-%m-%d} {clock(now_ny())} New York",
             f"TELEGRAM_TOKEN: {shape(RAW_TG_TOKEN, r'[0-9]+:[A-Za-z0-9_-]{30,}')}",
             f"TELEGRAM_CHAT_ID: {shape(RAW_TG_CHAT, r'-?[0-9]+')}"]
    if TG_TOKEN:
        me = telegram("getMe")
        lines.append("Token accepted by Telegram: " + (f"yes (@{me['result'].get('username', '?')})" if me.get("ok")
                                                      else f"NO ({hide_secrets(str(me.get('description')))})"))
        if TG_CHAT and me.get("ok"):
            sent = telegram("sendMessage", {"chat_id": TG_CHAT,
                                            "text": "✅ Telegram check: the bot can reach you. Updates will arrive here."})
            lines.append("Test message delivered: " + ("yes" if sent.get("ok")
                                                       else f"NO ({hide_secrets(str(sent.get('description')))})"))
    path = write_report("telegram-check.txt", [hide_secrets(x) for x in lines], mode="w")
    print("\n".join(lines))
    print(f"Saved {path}")


def run_notify():
    text = os.environ.get("MESSAGE", "").strip()
    if text:
        send(text)
    else:
        print("No message to send.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    try:
        {"live": run_live, "backtest": run_backtest, "notify": run_notify,
         "telegram_check": run_telegram_check}[mode]()
    except Exception as e:
        send(f"⚠️ Day trader error ({mode}): {e}")
        raise
    finally:
        if mode == "live" and LOG:
            write_report(f"live-{now_ny().date()}.txt", LOG)
