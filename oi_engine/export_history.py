"""Export recent Binance USD-M 15m candles and OI history to backtest CSV files.

Uses public market-data endpoints only; no API key is required. It does not
attempt to bypass regional restrictions. Binance OI history is capped at 500
samples per request, so this export is a short recent sample, not long-term
strategy validation.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .backtest import download_history
from .signal_engine import MarketDataError

CANDLE_FIELDS = [
    "open_time", "close_time", "open", "high", "low", "close",
    "volume", "quote_volume", "taker_buy_quote",
]
OI_FIELDS = ["timestamp", "sumOpenInterest"]


def export_symbol(symbol: str, output_dir: str = ".") -> tuple[Path, Path]:
    """Download and write the exact CSV schema accepted by the backtest."""
    normalized = symbol.upper().strip()
    if not normalized.endswith("USDT") or not normalized[:-4].isalnum():
        raise MarketDataError("Symbol must be a USDT perpetual, e.g. BTCUSDT")

    candles, oi_rows = download_history(normalized)
    if len(candles) < 22:
        raise MarketDataError(
            f"{normalized}: only {len(candles)} closed candles returned; "
            "at least 22 are required"
        )
    if len(oi_rows) < 3:
        raise MarketDataError(
            f"{normalized}: only {len(oi_rows)} OI samples returned; at least 3 are required"
        )

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    candles_path = destination / f"{normalized.lower()}_candles.csv"
    oi_path = destination / f"{normalized.lower()}_oi.csv"

    with candles_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDLE_FIELDS)
        writer.writeheader()
        for candle in candles:
            writer.writerow({field: candle[field] for field in CANDLE_FIELDS})

    with oi_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OI_FIELDS)
        writer.writeheader()
        for row in sorted(oi_rows, key=lambda item: int(item["timestamp"])):
            writer.writerow({
                "timestamp": int(row["timestamp"]),
                "sumOpenInterest": row["sumOpenInterest"],
            })

    print(f"Exported {normalized}: {len(candles)} closed 15m candles, {len(oi_rows)} OI samples")
    print(f"Candles CSV: {candles_path}")
    print(f"OI CSV: {oi_path}")
    print("Note: Binance OI history is limited to at most 500 samples per request.")
    print("Run the backtest with these two files; no API key is used.")
    return candles_path, oi_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export recent Binance USD-M 15m candles and OI history for backtesting"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="USDT perpetual symbol (default: BTCUSDT)")
    parser.add_argument("--output-dir", default=".", help="Directory for the two CSV files")
    args = parser.parse_args()
    try:
        candles_path, oi_path = export_symbol(args.symbol, args.output_dir)
    except (MarketDataError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(
        f'Next: python -m oi_engine.backtest --symbol {args.symbol.upper()} '
        f'--candles-csv "{candles_path}" --oi-csv "{oi_path}"'
    )


if __name__ == "__main__":
    main()
