"""Independent 4H/1D Binance Spot high-timeframe scanner.

This module is intentionally standalone: it does not import or modify the
existing 15m signal engine. It scans liquid Binance Spot USDT pairs, detects
closed-candle liquidity sweeps, calculates structural stops, and only emits
setups with at least 5% room to the first target and a structure-based second
target at 8% or more. SHORT alerts are analytical setups; spot accounts cannot
open short positions without margin/derivatives.
"""
from __future__ import annotations

import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Optional

import requests

BASE = "https://data-api.binance.vision"
TIMEFRAMES = ("4h", "1d")
MIN_TP1_PCT = 5.0
MIN_TP2_PCT = 8.0
SCAN_INTERVAL = max(300, int(os.getenv("HIGH_TF_SCAN_INTERVAL_SECONDS", "1800")))
MAX_SYMBOLS = max(10, int(os.getenv("HIGH_TF_MAX_SYMBOLS", "100")))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PumpHunter-HighTF/1.0"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class Setup:
    symbol: str
    timeframe: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    room_to_tp1_pct: float
    room_to_tp2_pct: float
    sweep_level: float
    confidence: int


def get_json(path: str, params: Optional[dict] = None):
    response = SESSION.get(BASE + path, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def universe() -> list[str]:
    info = get_json("/api/v3/exchangeInfo")
    allowed = {
        x["symbol"] for x in info.get("symbols", [])
        if x.get("status") == "TRADING"
        and x.get("isSpotTradingAllowed", True)
        and x.get("quoteAsset") == "USDT"
        and x.get("baseAsset") not in {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI"}
    }
    tickers = get_json("/api/v3/ticker/24hr")
    ranked = []
    for row in tickers:
        symbol = row.get("symbol")
        if symbol not in allowed:
            continue
        try:
            turnover = float(row.get("quoteVolume", 0))
            price = float(row.get("lastPrice", 0))
            if turnover >= 1_000_000 and price > 0:
                ranked.append((turnover, symbol))
        except (TypeError, ValueError):
            continue
    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:MAX_SYMBOLS]]


def candles(symbol: str, interval: str) -> list[dict]:
    rows = get_json("/api/v3/klines", {
        "symbol": symbol, "interval": interval, "limit": 160
    })
    now = int(time.time() * 1000)
    duration = 4 * 60 * 60 * 1000 if interval == "4h" else 24 * 60 * 60 * 1000
    result = []
    for r in rows:
        if int(r[0]) + duration - 1 >= now:
            continue  # never analyze an unfinished candle
        result.append({
            "open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            "close_time": int(r[6]), "quote_volume": float(r[7]),
        })
    if len(result) < 60:
        raise ValueError(f"insufficient closed {interval} candles")
    return result


def ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = alpha * x + (1 - alpha) * value
    return value


def atr(cs: list[dict], period: int = 14) -> float:
    ranges = []
    for prev, cur in zip(cs[-period-1:-1], cs[-period:]):
        ranges.append(max(cur["high"]-cur["low"],
                          abs(cur["high"]-prev["close"]),
                          abs(cur["low"]-prev["close"])))
    return sum(ranges) / len(ranges)


def analyze(symbol: str, tf: str) -> Optional[Setup]:
    cs = candles(symbol, tf)
    last = cs[-1]
    closes = [c["close"] for c in cs]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    a = atr(cs)
    if not math.isfinite(a) or a <= 0:
        return None

    # Recent 12-candle liquidity sweep followed by a closed-candle reclaim.
    for idx in range(len(cs)-1, max(50, len(cs)-5), -1):
        sweep = cs[idx]
        prior = cs[idx-12:idx]
        if len(prior) != 12:
            continue
        prior_low = min(x["low"] for x in prior)
        prior_high = max(x["high"] for x in prior)
        span = max(sweep["high"]-sweep["low"], 1e-12)
        long_sweep = (sweep["low"] < prior_low and sweep["close"] > prior_low
                      and sweep["close"] > sweep["open"]
                      and (sweep["close"]-sweep["low"])/span >= .60)
        short_sweep = (sweep["high"] > prior_high and sweep["close"] < prior_high
                       and sweep["close"] < sweep["open"]
                       and (sweep["high"]-sweep["close"])/span >= .60)
        if long_sweep and not short_sweep and e20 > e50 and last["close"] > e20:
            side, level, extreme = "LONG", prior_low, sweep["low"]
        elif short_sweep and not long_sweep and e20 < e50 and last["close"] < e20:
            side, level, extreme = "SHORT", prior_high, sweep["high"]
        else:
            continue

        entry = last["close"]
        # Structural invalidation beyond the sweep extreme, with ATR buffer.
        stop = extreme - .25*a if side == "LONG" else extreme + .25*a
        if (side == "LONG" and not 0 < stop < entry) or (side == "SHORT" and not stop > entry):
            continue

        # TP1 is 5%; TP2 uses a genuine later swing level if it offers >=8%.
        future_structure = cs[-61:-1]
        if side == "LONG":
            resistance = sorted(x["high"] for x in future_structure if x["high"] > entry)
            structural_tp2 = next((x for x in resistance if (x/entry-1)*100 >= MIN_TP2_PCT), None)
            if structural_tp2 is None:
                continue
            tp1 = entry * 1.05
            tp2 = structural_tp2
            room1, room2 = (tp1/entry-1)*100, (tp2/entry-1)*100
        else:
            support = sorted((x["low"] for x in future_structure if 0 < x["low"] < entry), reverse=True)
            structural_tp2 = next((x for x in support if (1-x/entry)*100 >= MIN_TP2_PCT), None)
            if structural_tp2 is None:
                continue
            tp1 = entry * .95
            tp2 = structural_tp2
            room1, room2 = (1-tp1/entry)*100, (1-tp2/entry)*100

        risk_pct = abs(entry-stop)/entry*100
        if risk_pct <= 0 or risk_pct > 8:
            continue
        return Setup(symbol, tf, side, entry, stop, tp1, tp2,
                     room1, room2, level, 70)
    return None


def fmt(x: float) -> str:
    return f"{x:.10g}"


def message(s: Setup) -> str:
    action = "خرید اسپات" if s.side == "LONG" else "هشدار SHORT (قابل اجرا در اسپات نیست)"
    return (
        f"📊 HIGH-TF {s.side} {s.symbol}\n"
        f"مرجع قیمت: Binance Spot | مرجع تحلیل: {s.timeframe}\n"
        f"نوع موقعیت: {action}\n"
        f"ورود پیشنهادی: {fmt(s.entry)}\n"
        f"حد ضرر ساختاری: {fmt(s.stop)} | ریسک: {abs(s.entry-s.stop)/s.entry*100:.2f}%\n"
        f"TP1: {fmt(s.tp1)} | فاصله: {s.room_to_tp1_pct:.2f}%\n"
        f"TP2 ساختاری: {fmt(s.tp2)} | فاصله: {s.room_to_tp2_pct:.2f}%\n"
        f"سطح شکار نقدینگی: {fmt(s.sweep_level)}\n"
        f"روش: Price Action + Liquidity Sweep + EMA20/50\n"
        f"فقط کندل‌های بسته‌شده بررسی شده‌اند. این سیگنال تضمین سود نیست."
    )


def send_telegram(text: str) -> None:
    token = os.getenv("HIGH_TF_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    chat_id = os.getenv("HIGH_TF_CHAT_ID") or os.getenv("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("HIGH_TF_BOT_TOKEN/HIGH_TF_CHAT_ID or BOT_TOKEN/CHAT_ID required")
    response = SESSION.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")


def run_once() -> dict:
    symbols = universe()
    sent, errors = [], 0
    for symbol in symbols:
        for tf in TIMEFRAMES:
            try:
                setup = analyze(symbol, tf)
                if setup:
                    send_telegram(message(setup))
                    sent.append(f"{symbol}:{tf}:{setup.side}")
            except Exception as exc:
                errors += 1
                logging.warning("%s %s: %s", symbol, tf, exc)
    return {"symbols": len(symbols), "sent": sent, "errors": errors}


def main() -> None:
    logging.info("Independent 4H/1D scanner started; max_symbols=%s interval=%ss",
                 MAX_SYMBOLS, SCAN_INTERVAL)
    while True:
        started = time.monotonic()
        try:
            result = run_once()
            logging.info("high timeframe scan: %s", result)
        except Exception:
            logging.exception("high timeframe scan failed")
        time.sleep(max(5, SCAN_INTERVAL-(time.monotonic()-started)))


if __name__ == "__main__":
    main()
