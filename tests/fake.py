"""Offline test harness: fake Alpaca, fake clock, fake GitHub restart API."""
import datetime as dt
import io
import json
import os
import random
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import daytrader as D  # noqa: E402

ORIG_SEND = D.send

NY = D.NY


class World:
    def __init__(self, days, symbols, seed=1, scenario=None, close=dt.time(16, 0), holidays=()):
        random.seed(seed)
        self.days = [d for d in days if d not in holidays]
        self.symbols = symbols
        self.close = close
        everyone = list(dict.fromkeys(symbols + ["SPY"]))  # SPY once, even if listed in symbols
        self.minute, self.daily = {}, {s: [] for s in everyone}
        start = days[0] - dt.timedelta(days=70)
        for s in everyone:
            p = random.uniform(50, 300)
            d = start
            while d <= days[-1]:
                if d.weekday() < 5 and d not in holidays:
                    o, c = p, p * (1 + random.gauss(0.0005, 0.015))
                    self.daily[s].append({"t": dt.datetime.combine(d, dt.time(0), NY), "o": o,
                                          "h": max(o, c) * 1.005, "l": min(o, c) * 0.995, "c": c, "v": 1_000_000})
                    if d in self.days:
                        bars = self.make_day(s, d, p, scenario)
                        self.minute[(s, d)] = bars
                        c = bars[-1]["c"]
                        self.daily[s][-1].update(o=bars[0]["o"], c=c, h=max(b["h"] for b in bars),
                                                 l=min(b["l"] for b in bars))
                    p = c
                d += dt.timedelta(days=1)
        self.now = None
        self.pos = None
        self.orders = {}
        self.dispatches = []

    def make_day(self, s, d, prev, scenario):
        bars, t, p = [], dt.datetime.combine(d, dt.time(9, 30), NY), prev * 1.01
        mode = scenario(s, d) if scenario else random.choice(["up", "down", "chop"])
        vol = 60_000 if mode != "chop" else 15_000
        n = int((dt.datetime.combine(d, self.close) - dt.datetime.combine(d, dt.time(9, 30))).total_seconds() // 60)
        for i in range(n):
            drift = {"up": 0.0006 if i < 15 else (0.0012 if i < 90 else 0.0),
                     "down": 0.0004 if i < 15 else -0.0012}.get(mode, 0)
            o, c = p, p * (1 + drift + random.gauss(0, 0.0008))
            bars.append({"t": t, "o": o, "h": max(o, c) * 1.0005, "l": min(o, c) * 0.9995, "c": c, "v": vol})
            p, t = c, t + dt.timedelta(minutes=1)
        return bars

    def price(self, s):
        bars = self.minute[(s, self.now.date())]
        done = [b for b in bars if b["t"] <= self.now - dt.timedelta(minutes=1)]
        return done[-1]["c"] if done else bars[0]["o"]


W = None


def fmt(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_api(method, url, params=None, body=None):
    path = url.split(".markets")[-1]
    if path == "/v2/calendar":
        s, e = dt.date.fromisoformat(params["start"]), dt.date.fromisoformat(params["end"])
        return [{"date": d.isoformat(), "open": "09:30", "close": W.close.strftime("%H:%M")}
                for d in W.days if s <= d <= e]
    if path == "/v2/stocks/bars":
        st = dt.datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        en = dt.datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        out = {}
        for s in params["symbols"].split(","):
            if params["timeframe"] == "1Day":
                src = W.daily.get(s, [])
            else:
                src = [b for d in W.days for b in W.minute.get((s, d), [])]
            sel = [b for b in src if st <= b["t"] <= en]
            if W.now is not None and params["timeframe"] == "1Min":
                sel = [b for b in sel if b["t"] <= W.now]
            out[s] = [dict(b, t=fmt(b["t"])) for b in sel]
        return {"bars": out, "next_page_token": None}
    if path == "/v2/stocks/trades/latest":
        s = params["symbols"]
        return {"trades": {s: {"p": W.price(s)}}}
    if path == "/v2/orders" and method == "POST":
        oid, s = f"o{len(W.orders)}", body["symbol"]
        p = W.price(s)
        qty = float(body["notional"]) / p if "notional" in body else float(body["qty"])
        W.orders[oid] = {"id": oid, "status": "filled", "filled_qty": str(qty), "filled_avg_price": str(p)}
        W.pos = (s, qty)
        return W.orders[oid]
    if path.startswith("/v2/orders/"):
        return W.orders[path.split("/")[-1]]
    if path.startswith("/v2/positions/") and method == "DELETE":
        s, oid = path.split("/")[-1], f"o{len(W.orders)}"
        W.orders[oid] = {"id": oid, "status": "filled", "filled_qty": str(W.pos[1]),
                         "filled_avg_price": str(W.price(s))}
        return W.orders[oid]
    raise RuntimeError(f"unhandled {method} {path}")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None, data=None):
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    if "api.github.com" in url:
        W.dispatches.append({"at": W.now, "url": url, "body": json.loads(req.data),
                             "auth": req.get_header("Authorization")})
        return FakeResp(b"")
    if "api.telegram.org" in url:
        raise AssertionError("tests must not call Telegram")
    raise AssertionError(f"unexpected network call {url}")


SENT = []


def install(world, start_time, on_github=True, job_start=None):
    """Point the bot at the fake world; clock starts at start_time (New York)."""
    global W
    W = world
    W.now = start_time
    D.api = fake_api
    D.now_ny = lambda: W.now

    def fsleep(sec):
        W.now += dt.timedelta(seconds=sec)
    D.sleep = fsleep
    urllib.request.urlopen = fake_urlopen
    D.TG_TOKEN = D.TG_CHAT = ""
    D.LOG.clear()
    SENT.clear()
    def capture(text):
        SENT.append(text)
        ORIG_SEND(text)
    D.send = capture
    for k in ("GITHUB_ACTIONS", "JOB_START", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_REF_NAME",
              "GITHUB_WORKFLOW_REF"):
        os.environ.pop(k, None)
    if on_github:
        os.environ.update({
            "GITHUB_ACTIONS": "true",
            "JOB_START": str((job_start or start_time).timestamp()),
            "GITHUB_REPOSITORY": "Mr-MTB/stock-assistant",
            "GITHUB_TOKEN": "test-token",
            "GITHUB_REF_NAME": "main",
            "GITHUB_WORKFLOW_REF": "Mr-MTB/stock-assistant/.github/workflows/assistant.yml@refs/heads/main",
        })


def run_main(mode="live"):
    """Same as `python daytrader.py <mode>`, including the report written at the end."""
    try:
        {"live": D.run_live, "backtest": D.run_backtest}[mode]()
    finally:
        if mode == "live" and D.LOG:
            D.write_report(f"live-{D.now_ny().date()}.txt", D.LOG)
