# Open Interest LONG/SHORT signal engine

This is a standalone 15-minute signal engine. It does not use the legacy Pump Hunter strategy modules.

## Signal rules

A final LONG or SHORT is emitted only when all of the following agree on the latest closed 15m candle:

- Open Interest value has increased by at least 0.35% over the two prior OI samples.
- Price has moved at least 0.15% in the signal direction.
- Taker-buy quote ratio is at least 53% for LONG or at most 47% for SHORT.
- Quote volume is at least 1.15x the preceding 20-candle average.
- The candle closes beyond the prior five-candle high/low and in the direction of the signal.

Stop loss uses 1.25 ATR (with a 0.2% minimum distance); targets are 1R and 2R. These are initial rule-based thresholds, not a measured win rate or a guarantee of profitability.

## Telegram setup

1. In the GitHub repository, open **Settings → Secrets and variables → Actions**.
2. Add repository secrets:
   - `BOT_TOKEN`: the Telegram bot token.
   - `CHAT_ID`: the destination chat ID.
3. Merge the pull request into the repository's default branch to enable the scheduled workflow.
4. Open **Actions → OI signal scanner → Run workflow** to trigger a manual scan.

The scanner is configured to run every five minutes and evaluates closed 15-minute candles. Duplicate alerts for the same symbol, direction, and candle are suppressed.

## Important limitations

- Market data is currently sourced from Binance USD-M Futures public endpoints.
- The OI history endpoint is sampled data; it is not a tick-by-tick liquidation or order-flow feed.
- The engine has unit tests, but no historical backtest, paper-trading validation, or live Telegram delivery has yet been confirmed.
- A rule score is not a statistical probability. Do not treat it as a win-rate estimate.


## Historical backtest

Run a recent-data backtest (default: BTCUSDT):

```bash
python -m oi_engine.backtest --symbol BTCUSDT
```

Optional execution-cost assumptions can be changed with `--fee-bps` and
`--slippage-bps` (each is basis points per side). The JSON report includes
trade count, win rate, profit factor, expectancy/average R, max drawdown in R,
LONG/SHORT counts, and individual trade outcomes.

The backtest uses only closed 15m candles and OI samples timestamped no later
than each signal candle. It does not stack positions in the same symbol.
Exits assume 50% at TP1 and 50% at TP2, with a fixed stop; if stop and target
are both touched in one OHLC candle, stop is counted first. Historical funding
is excluded. Binance's OI history endpoint is capped at 500 samples in this
implementation, so this is only a short recent sample and must not be presented
as a robust long-term win rate. Run multiple symbols and longer, independently
collected data before drawing conclusions.
