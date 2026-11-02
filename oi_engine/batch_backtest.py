"""Run the OI backtest across matching per-symbol CSV exports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import (
    DEFAULT_FEE_BPS,
    DEFAULT_SLIPPAGE_BPS,
    load_csv_history,
    run_backtest_data,
    summarize,
)
from .signal_engine import MarketDataError


def run_batch_backtest(
    symbols: list[str],
    data_dir: str = ".",
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    root = Path(data_dir)
    reports: dict[str, dict] = {}
    failures: dict[str, str] = {}
    pooled_trades: list[dict] = []

    for raw_symbol in symbols:
        symbol = raw_symbol.upper().strip()
        if not symbol.endswith("USDT") or not symbol[:-4].isalnum():
            failures[symbol] = "Symbol must be a USDT perpetual"
            continue
        candles_path = root / f"{symbol.lower()}_candles.csv"
        oi_path = root / f"{symbol.lower()}_oi.csv"
        if not candles_path.is_file() or not oi_path.is_file():
            failures[symbol] = f"Missing CSV pair: {candles_path.name}, {oi_path.name}"
            continue
        try:
            candles, oi_rows = load_csv_history(str(candles_path), str(oi_path))
            report = run_backtest_data(symbol, candles, oi_rows, fee_bps, slippage_bps)
            reports[symbol] = {
                key: value for key, value in report.items() if key != "trades_detail"
            }
            pooled_trades.extend(report["trades_detail"])
        except (MarketDataError, OSError, ValueError, KeyError) as exc:
            failures[symbol] = str(exc)

    return {
        "timeframe": "15m",
        "symbols_requested": [item.upper().strip() for item in symbols],
        "symbols_succeeded": list(reports),
        "symbols_failed": failures,
        "per_symbol": reports,
        "pooled_equal_risk_metrics": summarize(pooled_trades),
        "pooled_trade_count": len(pooled_trades),
        "pooled_metrics_note": (
            "Pooled results combine each symbol's R-multiple trades as if each "
            "trade risked an equal fraction independently. They are not a "
            "portfolio equity curve and do not model simultaneous capital use."
        ),
        "funding_included": False,
        "limitations": [
            "Funding is excluded; fees and slippage are approximations.",
            "Only the supplied CSV history is tested; sample length and market regimes matter.",
            "A pooled R-multiple summary is not a portfolio simulation.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch backtest OI signals from CSV exports")
    parser.add_argument(
        "--symbols", nargs="+",
        default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    )
    parser.add_argument("--data-dir", default="./oi-data")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--output", default="./oi-data/batch_backtest_report.json")
    args = parser.parse_args()
    report = run_batch_backtest(args.symbols, args.data_dir, args.fee_bps, args.slippage_bps)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "symbols_succeeded": report["symbols_succeeded"],
        "symbols_failed": report["symbols_failed"],
        "pooled_trade_count": report["pooled_trade_count"],
        "pooled_equal_risk_metrics": report["pooled_equal_risk_metrics"],
    }, ensure_ascii=False, indent=2))
    print(f"Full report: {output}")
    if not report["symbols_succeeded"]:
        parser.exit(2, "No symbol had a valid CSV pair; no backtest was run.\\n")


if __name__ == "__main__":
    main()
