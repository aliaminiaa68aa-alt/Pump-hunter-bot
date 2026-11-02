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
- The OI history endpoint is sampled data; it is not a tick-by-tick liquidation or order-flow feed. The signal engine measures percentage change in `sumOpenInterest` (open-contract quantity), not `sumOpenInterestValue` (USD notional), so ordinary price movement does not mechanically inflate the OI-growth filter. Binance timestamps OI statistics at the end of each period; alignment accounts for the one-millisecond difference between the inclusive kline close time and the next 15m boundary.
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
separate LONG and SHORT win rates, profit factors, expectancy and drawdowns, plus individual trade outcomes.

The backtest uses only closed 15m candles and OI samples timestamped no later
than each signal candle. Entries are modeled at the next candle open (not the
signal candle close), while preserving the planned stop distance and 1R/2R
targets; the report records the entry gap. It does not stack positions in the
same symbol. Exits assume 50% at TP1 and 50% at TP2, with a fixed stop; if stop and target
are both touched intrabar in one OHLC candle, stop is counted first. Opening
gaps are processed before intrabar highs/lows: adverse stop gaps fill at the
worse opening price, and favorable target gaps receive opening-price
improvement. Historical funding is excluded. Binance's OI history endpoint is capped at 500 samples in this
implementation, so this is only a short recent sample and must not be presented
as a robust long-term win rate. Run multiple symbols and longer, independently
collected data before drawing conclusions.



### Export recent history to CSV

On a machine/network where Binance USD-M public market-data endpoints are legally
available, export the latest supported sample without API credentials:

```bash
python -m oi_engine.export_history --symbol BTCUSDT --output-dir ./oi-data
python -m oi_engine.backtest --symbol BTCUSDT \
  --candles-csv ./oi-data/btcusdt_candles.csv \
  --oi-csv ./oi-data/btcusdt_oi.csv \
  --output ./oi-data/btcusdt_report.json
```

The exporter writes the exact candle and OI CSV schemas accepted by the
backtest. It uses public endpoints only and does not bypass HTTP 451 or other
regional restrictions. Binance currently caps OI history at 500 samples per
request (about five days at 15m intervals); this is useful for a smoke test,
not robust long-term validation. Collect a longer authorized history before
making claims about win rate or profitability.

### Authorized CSV history import

If the hosted runner cannot access Binance, the backtest can use an authorized
historical export without changing the signal rules:

```bash
python -m oi_engine.backtest --symbol BTCUSDT \
  --candles-csv candles.csv --oi-csv oi.csv \
  --output oi_backtest_report.json
```

Both files must use Unix millisecond timestamps. The candle CSV must contain
these exact headers:

`open_time,close_time,open,high,low,close,volume,quote_volume,taker_buy_quote`

The OI CSV must contain:

`timestamp,sumOpenInterest`

The candle export must include the taker-buy quote volume field; ordinary OHLCV
exports that omit it are not equivalent and cannot be used by this strategy.
Use only data you are authorized to access and export. The importer checks
required columns, duplicate timestamps, basic volume validity, and minimum
history length. It does not invent missing values or convert incompatible
provider-specific fields.

### Regional API availability

The historical backtest workflow is manual-only. The GitHub-hosted runner
returned HTTP 451 from Binance USD-M Futures for all tested symbols, which is
a Binance restricted-location response rather than a strategy result. No
win-rate or performance conclusion can be drawn from that run. Run the command
from a network where Binance USD-M public market data is legally available,
or use an authorized historical export. Do not route requests through an
unofficial third-party mirror to bypass geographic restrictions.
