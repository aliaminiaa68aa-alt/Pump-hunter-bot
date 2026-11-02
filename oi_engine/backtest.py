"""Historical, no-lookahead backtest for the standalone OI signal engine.

Binance's public openInterestHist endpoint is limited to recent history and
at most 500 samples per request. This tool reports the actual returned range;
it never labels a short sample as a long-term validation.
"""
from __future__ import annotations

import argparse
import json
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
        while cursor < len(ordered) and int(ordered[cursor].get("timestamp", 0)) <= candle["close_time"]:
            available.append(ordered[cursor])
            cursor += 1
        if not available:
            result.append(None)
            continue
        latest_ts = int(available[-1].get("timestamp", 0))
        if candle["close_time"] - latest_ts > INTERVAL_MS:
            result.append(None)
            continue
        last_three = available[-3:]
        if len(last_three) < 3:
            result.append(None)
            continue
        try:
            values = [_float(row.get("sumOpenInterestValue"), "OI value") for row in last_three]
        except MarketDataError:
            result.append(None)
            continue
        result.append(values if all(value > 0 for value in values) else None)
    return result


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
    k_response.raise_for_status()
    k_rows = k_response.json()
    oi_response = session.get(base + "/futures/data/openInterestHist",
                              params={"symbol": symbol, "period": "15m", "limit": 500},
                              timeout=20)
    oi_response.raise_for_status()
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


def run_backtest(symbol: str, fee_bps: float = DEFAULT_FEE_BPS,
                 slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> dict:
    candles, oi_rows = download_history(symbol.upper())
    aligned = align_oi(candles, oi_rows)
    trades: List[dict] = []
    next_available_index = 0

    for index in range(21, len(candles) - 1):
        if index < next_available_index or aligned[index] is None:
            continue
        snapshot = {
            "symbol": symbol.upper(),
            "candles": candles[:index + 1],
            "oi_values": aligned[index],
            "oi_timestamp": min(
                int(row["timestamp"]) for row in sorted(
                    [r for r in oi_rows if int(r.get("timestamp", 0)) <= candles[index]["close_time"]],
                    key=lambda r: int(r.get("timestamp", 0))
                )[-3:]
            ) if len([r for r in oi_rows if int(r.get("timestamp", 0)) <= candles[index]["close_time"]]) >= 3 else None,
            "funding_rate": 0.0,  # Historical funding is not available in this OI-only sample.
        }
        # evaluate_snapshot checks freshness of the latest OI timestamp.
        eligible = [r for r in oi_rows if int(r.get("timestamp", 0)) <= candles[index]["close_time"]]
        if len(eligible) < 3:
            continue
        snapshot["oi_timestamp"] = max(int(r["timestamp"]) for r in eligible)
        signal = evaluate_snapshot(snapshot)
        if signal is None:
            continue
        trade = simulate_trade(signal, candles[index + 1:], fee_bps, slippage_bps)
        if trade is None:
            continue
        trades.append(trade)
        # Do not stack same-symbol positions; resume after this trade's max holding window.
        next_available_index = index + min(MAX_HOLD_CANDLES, len(candles) - index - 1) + 1

    first_time = candles[0]["open_time"] if candles else None
    last_time = candles[-1]["close_time"] if candles else None
    return {
        "symbol": symbol.upper(),
        "timeframe": "15m",
        "candles": len(candles),
        "oi_samples": len(oi_rows),
        "history_start_ms": first_time,
        "history_end_ms": last_time,
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "exit_model": "50% TP1 + 50% TP2; fixed stop; stop-first on same-candle ambiguity",
        "funding_included": False,
        "metrics": summarize(trades),
        "trades_detail": trades,
        "limitations": [
            "Binance OI history endpoint returns at most 500 recent samples here.",
            "This is a short recent sample, not a robust multi-market regime test.",
            "Funding is excluded because historical funding was not fetched.",
            "Fees and slippage are configurable approximations; actual fills differ.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the 15m OI signal engine")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--output", default="oi_backtest_report.json")
    args = parser.parse_args()
    report = run_backtest(args.symbol, args.fee_bps, args.slippage_bps)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "trades_detail"},
                     ensure_ascii=False, indent=2))
    print("Full report:", args.output)


if __name__ == "__main__":
    main()
