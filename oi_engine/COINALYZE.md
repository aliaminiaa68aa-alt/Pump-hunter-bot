# Coinalyze API integration foundation

This module adds a bounded, authenticated Coinalyze data client for the standalone OI engine.

## Secret

The GitHub Actions repository secret is named \`COINALYZE_API_KEY\`. The key is read from the environment and sent in the \`api_key\` request header. Never paste the key into source code, logs, issues, pull requests, or this chat.

## Supported data

For a Binance USDT perpetual symbol, the client resolves the corresponding Coinalyze market and fetches:
- current open interest (USD-converted where supported) and 15-minute OI history;
- current and predicted funding rates;
- long/short ratio history;
- long and short liquidation history;
- OHLCV history and buy-volume ratio.

Missing data is returned as null and accompanied by per-series point counts. A missing metric must not be interpreted as zero or as confirmation.

## API limits and behavior

Coinalyze documents a maximum of 40 API requests per minute per key. The client applies a rolling-window throttle and honors \`Retry-After\` for HTTP 429. It requests only a six-hour, 15-minute history window per symbol.

Official reference: https://api.coinalyze.net/v1/doc/

## Current integration status

This is the provider/client foundation and its unit tests. It is not yet connected to an external Telegram OI alert source or enabled in the scheduled scanner. That inbound alert source must be connected before the requested "external first alert → watchlist → Coinalyze confirmation → final LONG/SHORT" workflow can be considered complete. No live trading or order placement is performed.
