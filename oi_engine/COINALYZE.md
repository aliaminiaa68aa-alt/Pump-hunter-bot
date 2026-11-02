# Coinalyze API integration foundation

This module adds the authenticated Coinalyze client and the alert-driven 15m OI watchlist workflow.

## Secret

The GitHub Actions repository secret is named `COINALYZE_API_KEY`. The key is read from the environment and sent in the `api_key` request header. Never paste the key into source code, logs, issues, pull requests, or this chat.

## Supported data

For a Binance USDT perpetual symbol, the client resolves the corresponding Coinalyze market and fetches:
- current open interest (USD-converted where supported) and 15-minute OI history;
- current and predicted funding rates;
- long/short ratio history;
- long and short liquidation history;
- OHLCV history and buy-volume ratio.

Missing data is returned as null and accompanied by per-series point counts. A missing metric must not be interpreted as zero or as confirmation.

## API limits and behavior

Coinalyze documents a maximum of 40 API requests per minute per key. The client applies a rolling-window throttle and honors `Retry-After` for HTTP 429. It requests only a six-hour, 15-minute history window per symbol.

Official reference: https://api.coinalyze.net/v1/doc/

## Alert-driven watchlist

1. The scheduled runner polls the configured Telegram bot inbox using `BOT_TOKEN`.
2. Forward the original Open Interest alert message from its source channel/bot into the private chat with this bot. Only messages carrying Telegram forward metadata are accepted; plain typed symbols are ignored.
3. The symbol is added to a persistent watchlist. Up to four symbols are monitored at once to stay below Coinalyze's documented 40-request/minute limit. Forwarded alerts older than six hours are ignored; watchlist entries expire after 24 hours unless refreshed.
4. Each watched symbol is evaluated on a closed Binance 15m candle and independently enriched with Coinalyze OI, funding, long/short ratio, liquidations and taker buy/sell volume.
5. A final LONG/SHORT notification is sent only when the existing OI engine signal and Coinalyze OI/taker-flow confirmation agree. Missing Coinalyze data fails closed.

Send `/status` or `/watchlist` to the bot to receive the current watchlist status.

## Operational constraints

- Telegram Bot API polling (`getUpdates`) cannot be used while a webhook is configured for the same bot. The runner intentionally does not remove an existing webhook automatically.
- This design requires the user to forward the external source alert into the bot inbox; Telegram bots cannot silently read messages from another bot's private chat.
- The scheduled workflow is still gated by merging the PR into the intended branch and enabling the Actions workflow. This code does not place trades or prove a strategy win rate.
