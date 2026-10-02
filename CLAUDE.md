# Day trader bot: notes for Claude

Paper-trading bot for US stocks. `daytrader.py` runs on GitHub Actions
(`.github/workflows/assistant.yml`) with an Alpaca **paper** account and sends alerts to
the owner's Telegram. Any real-money trades are placed by hand in the Sahm app, never by
this code.

## Fixed rules (change only when the owner asks)
- Long only, opening-range breakout, at most one trade per day.
- Every trade closes within 2 hours. Medium risk: about 3% of the account lost at the stop.
- Fees: Sahm 0.105% per order. Messages show Saudi time (Asia/Riyadh).
- Paper trading only. Never add real-money order placement.

## Where results live
- `daytrades.json`: paper account balance and every live trade.
- `reports/live-YYYY-MM-DD.txt`: everything the bot reported that day, with times.
- `reports/backtest-YYYY-MM-DD.txt`: backtest summary, every trade, day by day.

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
