"""Historical, no-lookahead backtest for the standalone OI signal engine.

Binance's public openInterestHist endpoint is limited to recent history and
at most 500 samples per request. This tool reports the actual returned range;
it never labels a short sample as a long-term validation.
"""
from __future__ import annotations

import argparse
import json
import csv
from dataclasses import replace
from typing import Any, Dict, List, Optional

import requests

from .signal_engine import BASE_URL, MarketDataError, _float, evaluate_snapshot

INTERVAL_MS = 15 * 60 * 1000
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 2.0
MAX_HOLD_CANDLES = 32


def parse_klines(rows: List[list]) -> List[dict]:
    parsed = []
    for row in rows:
        parsed.append({
            "open_time": int(row[0]),
            "open": _float(row[1], "open"),
            "high": _float(row[2], "high"),
            "low": _float(row[3], "low"),
            "close": _float(row[4], "close"),
            "volume": _float(row[5], "volume"),
            "quote_volume": _float(row[7], "quote volume"),
            "taker_buy_quote": _float(row[10], "taker buy quote"),
            "close_time": int(row[6]),
        })
    return parsed


def align_oi(candles: List[dict], oi_rows: List[dict]) -> List[Optional[List[float]]]:
    """For each closed candle, return its three latest OI samples, without lookahead."""
    ordered = sorted(oi_rows, key=lambda row: int(row.get("timestamp", 0)))
    result: List[Optional[List[float]]] = []
    cursor = 0
    available: List[dict] = []
    for candle in candles:
        # OI-statistics timestamp is the period END; kline close_time is
        # inclusive and therefore one millisecond before the next boundary.
        oi_cutoff_time = candle["close_time"] + 1
        while cursor < len(ordered) and int(ordered[cursor].get("timestamp", 0)) <= oi_cutoff_time:
            available.append(ordered[cursor])
            cursor += 1
        if not available:
            result.append(None)
            continue
        latest_ts = int(available[-1].get("timestamp", 0))
        if oi_cutoff_time - latest_ts > INTERVAL_MS:
            result.append(None)
            continue
        last_three = available[-3:]
        if len(last_three) < 3:
            result.append(None)
            continue
        # OI inputs must be a genuine 15m sequence, not three stale samples
        # separated by missing intervals.
        oi_times = [int(row.get("timestamp", 0)) for row in last_three]
        if any(
            current - previous != INTERVAL_MS
            for previous, current in zip(oi_times, oi_times[1:])
        ):
            result.append(None)
            continue
        try:
            values = [_float(row.get("sumOpenInterest"), "OI value") for row in last_three]
        except MarketDataError:
            result.append(None)
            continue
        result.append(values if all(value > 0 for value in values) else None)
    return result


def reprice_signal_for_next_open(signal: Any, next_open: float) -> Any:
    """Move entry to the first executable bar open and preserve the planned R distance."""
    entry = _float(next_open, "next candle open")
    if entry <= 0:
        raise MarketDataError("Next candle open must be positive")
    risk = abs(float(signal.entry) - float(signal.stop_loss))
    if risk <= 0:
        raise MarketDataError("Signal risk distance must be positive")
    if signal.side == "LONG":
        stop, tp1, tp2 = entry - risk, entry + risk, entry + 2.0 * risk
    elif signal.side == "SHORT":
        stop, tp1, tp2 = entry + risk, entry - risk, entry - 2.0 * risk
    else:
        raise MarketDataError(f"Unsupported signal side: {signal.side}")
    return replace(signal, entry=entry, stop_loss=stop,
                   take_profit_1=tp1, take_profit_2=tp2)


def simulate_trade(signal: Any, future_candles: List[dict],
                   fee_bps: float = DEFAULT_FEE_BPS,
                   slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                   max_hold: int = MAX_HOLD_CANDLES) -> Optional[dict]:
    """50% exits at TP1 and 50% at TP2; stop is not moved after TP1.

    If stop and target are both touched in one candle, stop is assumed first.
    Remaining position is closed at the time limit. Returns net R after a
    configurable round-trip fee/slippage allowance, or None if no future bar.
    """
    if not future_candles:
        return None
    side = signal.side
    entry = signal.entry
    stop = signal.stop_loss
    tp1 = signal.take_profit_1
    tp2 = signal.take_profit_2
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    first_target_hit = False
    gross_r = 0.0
    exit_reason = "time"
    exit_price = future_candles[min(len(future_candles), max_hold) - 1]["close"]

    for candle in future_candles[:max_hold]:
        if side == "LONG":
            stop_hit = candle["low"] <= stop
            tp1_hit = candle["high"] >= tp1
            tp2_hit = candle["high"] >= tp2
        else:
            stop_hit = candle["high"] >= stop
            tp1_hit = candle["low"] <= tp1
            tp2_hit = candle["low"] <= tp2

        # Conservative ordering when OHLC cannot reveal which level came first.
        if stop_hit:
            if first_target_hit:
                # TP1's +0.5R was booked when first reached; only book
                # the remaining half stopped at -1R here.
                gross_r += 0.5 * -1.0
            else:
                gross_r = -1.0
            exit_reason = "stop_after_tp1" if first_target_hit else "stop"
            exit_price = stop
            break

        if tp2_hit:
            if not first_target_hit:
                gross_r += 0.5 * 1.0
            gross_r += 0.5 * 2.0
            exit_reason = "tp2"
            exit_price = tp2
            break

        if tp1_hit and not first_target_hit:
            first_target_hit = True
            gross_r += 0.5 * 1.0

        exit_price = candle["close"]
    else:
        # Time exit: mark remaining half at the last observed close.
        direction = 1.0 if side == "LONG" else -1.0
        if first_target_hit:
            gross_r += 0.5 * direction * (exit_price - entry) / risk
        else:
            gross_r = direction * (exit_price - entry) / risk

    # Approximate entry + exit trading costs on full notional.
    cost_r = (entry * 2.0 * (fee_bps + slippage_bps) / 10000.0) / risk
    net_r = gross_r - cost_r
    return {
        "symbol": signal.symbol,
        "side": side,
        "entry": entry,
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "gross_r": round(gross_r, 4),
        "cost_r": round(cost_r, 4),
        "net_r": round(net_r, 4),
        "exit_reason": exit_reason,
        "candle_close_time": signal.candle_close_time,
    }


def summarize(trades: List[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate_pct": None, "profit_factor": None,
                "expectancy_r": None, "average_r": None, "max_drawdown_r": None}
    values = [float(item["net_r"]) for item in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(values) - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / len(values) * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "expectancy_r": round(sum(values) / len(values), 4),
        "average_r": round(sum(values) / len(values), 4),
        "max_drawdown_r": round(max_drawdown, 4),
        "long_trades": sum(item["side"] == "LONG" for item in trades),
        "short_trades": sum(item["side"] == "SHORT" for item in trades),
    }


def download_history(symbol: str) -> tuple[List[dict], List[dict]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "OI-Signal-Backtest/1.0"})
    base = BASE_URL
    k_response = session.get(base + "/fapi/v1/klines",
                             params={"symbol": symbol, "interval": "15m", "limit": 500},
                             timeout=20)
    try:
        k_response.raise_for_status()
    except requests.HTTPError as exc:
        if k_response.status_code == 451:
            raise MarketDataError(
                "Binance USD-M Futures returned HTTP 451 (restricted location). "
                "No historical data was downloaded; run from a network where this "
                "market-data service is legally available, or provide an authorized data export."
            ) from exc
        raise
    k_rows = k_response.json()
    oi_response = session.get(base + "/futures/data/openInterestHist",
                              params={"symbol": symbol, "period": "15m", "limit": 500},
                              timeout=20)
    try:
        oi_response.raise_for_status()
    except requests.HTTPError as exc:
        if oi_response.status_code == 451:
            raise MarketDataError(
                "Binance USD-M Futures OI history returned HTTP 451 (restricted location)."
            ) from exc
        raise
    oi_rows = oi_response.json()
    if not isinstance(k_rows, list) or not isinstance(oi_rows, list):
        raise MarketDataError("Unexpected Binance history response")
    candles = parse_klines(k_rows)
    # Drop the currently-forming kline; only closed candles may generate signals.
    if candles:
        import time
        now_ms = int(time.time() * 1000)
        candles = [candle for candle in candles if candle["close_time"] < now_ms]
    return candles, oi_rows



def load_csv_history(candles_path: str, oi_path: str) -> tuple[List[dict], List[dict]]:
    """Load authorized historical exports using the documented CSV schema.

    candles.csv: open_time,close_time,open,high,low,close,volume,quote_volume,taker_buy_quote
    oi.csv: timestamp,sumOpenInterest
    Timestamps are Unix milliseconds.
    """
    required_candles = {
        "open_time", "close_time", "open", "high", "low", "close",
        "volume", "quote_volume", "taker_buy_quote",
    }
    candles: List[dict] = []
    with open(candles_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = required_candles - set(reader.fieldnames or [])
        if missing:
            raise MarketDataError(f"candles CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            item = {"open_time": int(row["open_time"]), "close_time": int(row["close_time"])}
            for key in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote"):
                item[key] = _float(row[key], key)
            if (
                item["close_time"] != item["open_time"] + INTERVAL_MS - 1
                or item["close"] <= 0
                or item["open"] <= 0
                or item["high"] < max(item["open"], item["close"])
                or item["low"] > min(item["open"], item["close"])
                or item["high"] < item["low"]
            ):
                raise MarketDataError("Invalid 15m candle timestamps or OHLC prices in CSV")
            if (
                item["volume"] < 0
                or item["quote_volume"] <= 0
                or item["taker_buy_quote"] < 0
                or item["taker_buy_quote"] > item["quote_volume"] * 1.001
            ):
                raise MarketDataError("Invalid quote/taker-buy volume in candles CSV")
            candles.append(item)

    oi_rows: List[dict] = []
    with open(oi_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required_oi = {"timestamp", "sumOpenInterest"}
        missing = required_oi - set(reader.fieldnames or [])
        if missing:
            raise MarketDataError(f"OI CSV missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            value = _float(row["sumOpenInterest"], "OI value")
            if value <= 0:
                raise MarketDataError("OI values must be positive")
            oi_rows.append({"timestamp": int(row["timestamp"]), "sumOpenInterest": value})

    if len(candles) < 22 or len(oi_rows) < 3:
        raise MarketDataError("CSV history is too short: need at least 22 candles and 3 OI samples")
    candle_times = [item["open_time"] for item in candles]
    if len(set(candle_times)) != len(candle_times):
        raise MarketDataError("Duplicate candle open_time values in CSV")
    if any(current <= previous for previous, current in zip(candle_times, candle_times[1:])):
        raise MarketDataError("Candles CSV rows must be in chronological order")
    if any(
        current["open_time"] - previous["open_time"] != INTERVAL_MS
        for previous, current in zip(candles, candles[1:])
    ):
        raise MarketDataError("Candles CSV has missing or irregular 15m intervals")
    oi_times = [item["timestamp"] for item in oi_rows]
    if len(set(oi_times)) != len(oi_times):
        raise MarketDataError("Duplicate OI timestamps in CSV")
    if any(current <= previous for previous, current in zip(oi_times, oi_times[1:])):
        raise MarketDataError("OI CSV rows must be in chronological order")
    return candles, oi_rows


def run_backtest_data(symbol: str, candles: List[dict], oi_rows: List[dict],
                      fee_bps: float = DEFAULT_FEE_BPS,
                      slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> dict:
    aligned = align_oi(candles, oi_rows)
    trades: List[dict] = []
    next_available_index = 0
    for index in range(21, len(candles) - 1):
        if index < next_available_index or aligned[index] is None:
            continue
        oi_cutoff_time = candles[index]["close_time"] + 1
        eligible = [r for r in oi_rows if int(r.get("timestamp", 0)) <= oi_cutoff_time]
        if len(eligible) < 3:
            continue
        snapshot = {
            "symbol": symbol.upper(),
            "candles": candles[:index + 1],
            "oi_values": aligned[index],
            "oi_timestamp": max(int(r["timestamp"]) for r in eligible),
            "funding_rate": 0.0,
        }
        signal = evaluate_snapshot(snapshot)
        if signal is None:
            continue
        executable_signal = reprice_signal_for_next_open(signal, candles[index + 1]["open"])
        trade = simulate_trade(executable_signal, candles[index + 1:], fee_bps, slippage_bps)
        if trade is None:
            continue
        trade["signal_close_entry"] = signal.entry
        trade["entry_gap_pct"] = round(
            (executable_signal.entry / signal.entry - 1.0) * 100.0, 4
        )
        trades.append(trade)
        next_available_index = index + min(MAX_HOLD_CANDLES, len(candles) - index - 1) + 1

    return {
        "symbol": symbol.upper(),
        "timeframe": "15m",
        "candles": len(candles),
        "oi_samples": len(oi_rows),
        "history_start_ms": candles[0]["open_time"] if candles else None,
        "history_end_ms": candles[-1]["close_time"] if candles else None,
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "exit_model": "Entry at next candle open; 50% TP1 + 50% TP2; fixed stop; stop-first on same-candle ambiguity",
        "funding_included": False,
        "metrics": summarize(trades),
        "trades_detail": trades,
        "limitations": [
            "Only closed 15m candles and OI samples timestamped no later than each signal candle are used.",
            "Entries are modeled at the next candle open, not the signal candle close; SL/TP distances are preserved from the signal plan.",
            "Funding is excluded from this backtest.",
            "Fees and slippage are configurable approximations; actual fills differ.",
        ],
    }

def run_backtest(symbol: str, fee_bps: float = DEFAULT_FEE_BPS,
                 slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                 candles_csv: Optional[str] = None,
                 oi_csv: Optional[str] = None) -> dict:
    if bool(candles_csv) != bool(oi_csv):
        raise MarketDataError("Both --candles-csv and --oi-csv must be supplied together")
    if candles_csv and oi_csv:
        candles, oi_rows = load_csv_history(candles_csv, oi_csv)
        report = run_backtest_data(symbol, candles, oi_rows, fee_bps, slippage_bps)
        report["data_source"] = "user_authorized_csv_export"
        return report
    candles, oi_rows = download_history(symbol.upper())
    report = run_backtest_data(symbol, candles, oi_rows, fee_bps, slippage_bps)
    report["data_source"] = "Binance USD-M Futures public API"
    report["limitations"].extend([
        "Binance OI history endpoint returns at most 500 recent samples in this implementation.",
        "This is a short recent sample, not a robust multi-market regime test.",
    ])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the 15m OI signal engine")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--output", default="oi_backtest_report.json")
    parser.add_argument("--candles-csv", help="Authorized 15m candles CSV export")
    parser.add_argument("--oi-csv", help="Authorized OI CSV export")
    args = parser.parse_args()
    try:
        report = run_backtest(args.symbol, args.fee_bps, args.slippage_bps,
                              args.candles_csv, args.oi_csv)
    except MarketDataError as exc:
        parser.exit(2, f"Backtest unavailable: {exc}\\n")
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "trades_detail"},
                     ensure_ascii=False, indent=2))
    print("Full report:", args.output)


if __name__ == "__main__":
    main()
