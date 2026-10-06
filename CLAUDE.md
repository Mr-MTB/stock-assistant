# Day trader bot: notes for Claude

Paper-trading bot for US stocks. `daytrader.py` runs on GitHub Actions
(`.github/workflows/assistant.yml`) with an Alpaca **paper** account and sends alerts to
the owner's Telegram. Any real-money trades are placed by hand in the Sahm app, never by
this code.

## Fixed rules (change only when the owner asks)
- Long only, opening-range breakout on the 5 busiest stocks of the morning, using the first
  5 minutes (9:30-9:35 AM New York) as the opening range.
- Up to 3 trades a day, a third of the day's money each (they may overlap); entries until
  2:50 PM New York; no new buys while the S&P 500 (SPY) is down more than 1% on the day.
- Stop at the middle of the opening range, no fixed target: every trade rides until the
  end-of-day sale at 3:20 PM New York (40 minutes before the close) unless the stop is hit.
  Never held overnight. Never more than 3% of the account at risk on one trade.
  (Owner's request on Oct 5, 2026: "hold longer, from the opening till before closing".
  Version chosen from 64 tested: reports/research-exits-2026-10-06.txt on claude/research.)
- Each pick shows the market, the stock's latest headlines and the setup's past success rate.
- Fees: Sahm 0.105% per order. Messages show Saudi time (Asia/Riyadh) in 12-hour format.
- Paper trading only, starting from 2,000 SAR. Never add real-money order placement.
- GitHub runs last at most 6 hours: the trading run starts at 9:28 AM so one run covers the
  day. If a run must stop with trades open, it hands them over to a fresh run (saved in
  daytrades.json under "open"); keep the live loop and `simulate_day` making the same trades.

## Where results live
- `daytrades.json`: paper account balance and every live trade.
- `reports/live-YYYY-MM-DD.txt`: everything the bot reported that day, with times.
- `reports/backtest-YYYY-MM-DD.txt`: backtest summary, every trade, day by day.
- `journal/`: the practice database (every trade, every breakout signal, every day).

Claude cloud sessions can't reach Alpaca, Telegram or the Actions logs, so read results
from these files (pull the repo first).

## Running and testing
- Live runs start from several cron wake-ups because GitHub often starts scheduled runs
  hours late. The bot handles early, on-time and late starts itself (see the docstring at
  the top of `daytrader.py`).
- Start a run by hand: POST `/repos/Mr-MTB/stock-assistant/actions/workflows/assistant.yml/dispatches`
  with `{"ref": "main", "inputs": {"mode": "backtest"}}` (or `"live"`).
- Message the owner on Telegram: same POST with `{"ref": "main", "inputs": {"mode": "notify", "message": "..."}}`.
  Start the text with "📋 Claude:" so it stands apart from the bot's own messages.
- `reports/setup-stats.json` holds the past success rate shown with each pick; every backtest refreshes it.
- Before pushing a change, run `python3 tests/scenarios.py` (fake Alpaca, fake clock,
  no network) and extend it for anything new.

## Working with the owner
- Explain in plain, non-technical language. Changes go through a pull request the owner merges.
- Recommend a strategy change only if it also holds up on recent weeks it wasn't tuned on.
