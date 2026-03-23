#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

LOGGER = logging.getLogger("swing_bot")


@dataclass
class SymbolConfig:
    symbol: str
    t212_ticker: Optional[str] = None


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    opened_at: Optional[str] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    breakout_price: Optional[float] = None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = {"sent_alerts": {}}
        if path.exists():
            self.state = json.loads(path.read_text())
            self.state.setdefault("sent_alerts", {})

    def seen(self, key: str) -> bool:
        return key in self.state["sent_alerts"]

    def mark(self, key: str, payload: Dict[str, Any]) -> None:
        self.state["sent_alerts"][key] = {
            "sent_at": dt.datetime.utcnow().isoformat() + "Z",
            **payload,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2, sort_keys=True))


class Notifier:
    def __init__(self) -> None:
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")

    def send(self, lines: Iterable[str]) -> None:
        text = "\n".join(lines).strip()
        if not text:
            return
        print(text)
        if self.telegram_token and self.telegram_chat_id:
            self._send_telegram(text)
        if self.discord_webhook:
            self._send_discord(text)

    def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": text}
        try:
            resp = requests.post(url, json=payload, timeout=20)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Telegram send failed: %s", exc)

    def _send_discord(self, text: str) -> None:
        try:
            resp = requests.post(self.discord_webhook, json={"content": text}, timeout=20)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Discord send failed: %s", exc)


class Trading212Client:
    def __init__(self, env_name: str, api_key: str, api_secret: str) -> None:
        env_name = env_name.lower().strip()
        if env_name not in {"demo", "live"}:
            raise ValueError("T212_ENV must be 'demo' or 'live'.")
        base = "https://demo.trading212.com/api/v0" if env_name == "demo" else "https://live.trading212.com/api/v0"
        self.base_url = base
        creds = base64.b64encode(f"{api_key}:{api_secret}".encode("utf-8")).decode("utf-8")
        self.headers = {"Authorization": f"Basic {creds}"}

    def fetch_positions(self) -> List[Position]:
        url = f"{self.base_url}/equity/positions"
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        items = resp.json()
        positions: List[Position] = []
        for item in items:
            instrument = item.get("instrument", {}) or {}
            symbol = (
                instrument.get("ticker")
                or instrument.get("symbol")
                or instrument.get("shortName")
                or instrument.get("name")
            )
            if not symbol:
                continue
            positions.append(
                Position(
                    symbol=str(symbol).split("_")[0],
                    quantity=float(item.get("quantity", 0) or 0),
                    entry_price=float(item.get("averagePricePaid", 0) or 0),
                    opened_at=item.get("createdAt"),
                )
            )
        return positions


class SwingSignalBot:
    def __init__(self, config_path: Path, positions_path: Path, state_path: Path) -> None:
        self.config_path = config_path
        self.positions_path = positions_path
        self.config = self._load_config(config_path)
        self.state = StateStore(state_path)
        self.notifier = Notifier()

    @staticmethod
    def _load_config(path: Path) -> Dict[str, Any]:
        raw = json.loads(path.read_text())
        required = [
            "symbols",
            "lookback_days",
            "breakout_lookback",
            "near_breakout_pct",
            "trend_fast_ma",
            "trend_slow_ma",
            "min_avg_dollar_volume",
            "min_atr_pct",
            "max_atr_pct",
            "volume_spike_multiple",
            "stop_pct",
            "target_pct",
            "time_exit_days",
        ]
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"Missing config keys: {missing}")
        return raw

    def load_positions(self) -> List[Position]:
        api_key = os.getenv("T212_API_KEY")
        api_secret = os.getenv("T212_API_SECRET")
        t212_env = os.getenv("T212_ENV", "demo")
        if api_key and api_secret:
            try:
                LOGGER.info("Loading positions from Trading 212 %s account", t212_env)
                return Trading212Client(t212_env, api_key, api_secret).fetch_positions()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Falling back to local positions file because Trading 212 fetch failed: %s", exc)

        if not self.positions_path.exists():
            return []
        data = json.loads(self.positions_path.read_text())
        return [Position(**item) for item in data]

    def save_positions_template(self) -> None:
        if self.positions_path.exists():
            return
        self.positions_path.write_text("[]\n")

    def fetch_price_history(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        period = max(int(self.config["lookback_days"]), int(self.config["trend_slow_ma"]) + 30)
        # Small pad because yfinance period strings are rough, so we convert days to months where possible.
        calendar_days = int(math.ceil(period * 1.6))
        start = (dt.date.today() - dt.timedelta(days=calendar_days)).isoformat()
        dataset = yf.download(
            tickers=symbols,
            start=start,
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        history: Dict[str, pd.DataFrame] = {}
        multi_ticker = isinstance(dataset.columns, pd.MultiIndex)
        for symbol in symbols:
            df = dataset[symbol].copy() if multi_ticker else dataset.copy()
            df = df.rename(columns=str).dropna(how="all")
            if df.empty:
                LOGGER.warning("No data returned for %s", symbol)
                continue
            history[symbol] = self._enrich_indicators(df)
        return history

    def _enrich_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        fast = int(self.config["trend_fast_ma"])
        slow = int(self.config["trend_slow_ma"])
        breakout_lb = int(self.config["breakout_lookback"])
        atr_period = int(self.config.get("atr_period", 14))
        avg_volume_window = int(self.config.get("avg_volume_window", 20))

        out = df.copy()
        out["close"] = out["Close"]
        out["high"] = out["High"]
        out["low"] = out["Low"]
        out["open"] = out["Open"]
        out["volume"] = out["Volume"]
        out["sma_fast"] = out["close"].rolling(fast).mean()
        out["sma_slow"] = out["close"].rolling(slow).mean()
        prev_close = out["close"].shift(1)
        tr_components = pd.concat(
            [
                out["high"] - out["low"],
                (out["high"] - prev_close).abs(),
                (out["low"] - prev_close).abs(),
            ],
            axis=1,
        )
        out["tr"] = tr_components.max(axis=1)
        out["atr"] = out["tr"].rolling(atr_period).mean()
        out["atr_pct"] = out["atr"] / out["close"]
        out["avg_volume"] = out["volume"].rolling(avg_volume_window).mean()
        out["avg_dollar_volume"] = (out["close"] * out["volume"]).rolling(avg_volume_window).mean()
        out["prior_breakout_high"] = out["high"].shift(1).rolling(breakout_lb).max()
        out["distance_to_breakout_pct"] = (out["prior_breakout_high"] - out["close"]) / out["prior_breakout_high"]
        return out.dropna()

    def run(self) -> None:
        symbols = [self._symbol_from_entry(entry).symbol for entry in self.config["symbols"]]
        history = self.fetch_price_history(symbols)
        positions = {position.symbol.upper(): position for position in self.load_positions()}
        self.save_positions_template()

        messages: List[str] = []
        today_key = dt.date.today().isoformat()

        for symbol in symbols:
            df = history.get(symbol)
            if df is None or df.empty:
                continue
            latest = df.iloc[-1]
            if symbol in positions:
                alert = self._build_exit_alert(symbol, latest, positions[symbol])
                if alert:
                    key = f"{today_key}:{symbol}:exit:{alert['reason']}"
                    if not self.state.seen(key):
                        self.state.mark(key, alert)
                        messages.append(self._format_exit_alert(alert))
            else:
                watch = self._build_watch_alert(symbol, latest)
                if watch:
                    key = f"{today_key}:{symbol}:watch"
                    if not self.state.seen(key):
                        self.state.mark(key, watch)
                        messages.append(self._format_watch_alert(watch))
                buy = self._build_buy_alert(symbol, latest)
                if buy:
                    key = f"{today_key}:{symbol}:buy"
                    if not self.state.seen(key):
                        self.state.mark(key, buy)
                        messages.append(self._format_buy_alert(buy))

        if messages:
            self.notifier.send(messages)
        else:
            self.notifier.send([
                f"✅ {self.config.get('strategy_name', 'swing_breakout_v1')} scan complete.",
                "No new buy/watch/exit signals today.",
            ])
        self.state.save()

    def _common_filters(self, latest: pd.Series) -> bool:
        in_trend = latest["close"] > latest["sma_fast"] > latest["sma_slow"]
        atr_ok = float(self.config["min_atr_pct"]) <= latest["atr_pct"] <= float(self.config["max_atr_pct"])
        dollar_vol_ok = latest["avg_dollar_volume"] >= float(self.config["min_avg_dollar_volume"])
        return bool(in_trend and atr_ok and dollar_vol_ok)

    def _build_watch_alert(self, symbol: str, latest: pd.Series) -> Optional[Dict[str, Any]]:
        if not self._common_filters(latest):
            return None
        distance = latest["distance_to_breakout_pct"]
        if pd.isna(distance) or distance < 0 or distance > float(self.config["near_breakout_pct"]):
            return None
        return {
            "symbol": symbol,
            "close": float(latest["close"]),
            "breakout": float(latest["prior_breakout_high"]),
            "atr_pct": float(latest["atr_pct"]),
            "avg_dollar_volume": float(latest["avg_dollar_volume"]),
            "distance": float(distance),
        }

    def _build_buy_alert(self, symbol: str, latest: pd.Series) -> Optional[Dict[str, Any]]:
        if not self._common_filters(latest):
            return None
        breakout = latest["prior_breakout_high"]
        if pd.isna(breakout):
            return None
        is_breakout = latest["close"] > breakout
        has_volume = latest["volume"] > (latest["avg_volume"] * float(self.config["volume_spike_multiple"]))
        green_candle = latest["close"] >= latest["open"]
        if not (is_breakout and has_volume and green_candle):
            return None
        stop = float(latest["close"]) * (1 - float(self.config["stop_pct"]))
        target = float(latest["close"]) * (1 + float(self.config["target_pct"]))
        return {
            "symbol": symbol,
            "close": float(latest["close"]),
            "breakout": float(breakout),
            "stop": stop,
            "target": target,
            "atr_pct": float(latest["atr_pct"]),
            "volume": float(latest["volume"]),
            "avg_volume": float(latest["avg_volume"]),
        }

    def _build_exit_alert(self, symbol: str, latest: pd.Series, position: Position) -> Optional[Dict[str, Any]]:
        close = float(latest["close"])
        stop = position.stop_price or (position.entry_price * (1 - float(self.config["stop_pct"])))
        target = position.target_price or (position.entry_price * (1 + float(self.config["target_pct"])))
        max_days = int(self.config["time_exit_days"])

        reason = None
        if close <= stop:
            reason = "stop_hit"
        elif close >= target:
            reason = "target_hit"
        elif bool(self.config.get("exit_on_fast_ma_break", True)) and close < float(latest["sma_fast"]):
            reason = "lost_fast_ma"
        elif position.opened_at:
            try:
                opened = dt.datetime.fromisoformat(position.opened_at.replace("Z", "+00:00")).date()
                if (dt.date.today() - opened).days >= max_days:
                    reason = "time_exit"
            except ValueError:
                pass

        if reason is None:
            return None
        pnl_pct = (close / position.entry_price) - 1 if position.entry_price else 0
        return {
            "symbol": symbol,
            "reason": reason,
            "close": close,
            "entry": position.entry_price,
            "stop": stop,
            "target": target,
            "pnl_pct": pnl_pct,
            "quantity": position.quantity,
        }

    @staticmethod
    def _symbol_from_entry(entry: Any) -> SymbolConfig:
        if isinstance(entry, str):
            return SymbolConfig(symbol=entry)
        return SymbolConfig(symbol=entry["symbol"], t212_ticker=entry.get("t212_ticker"))

    @staticmethod
    def _format_pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    def _format_watch_alert(self, alert: Dict[str, Any]) -> str:
        return (
            f"👀 WATCH {alert['symbol']} | close {alert['close']:.2f} | breakout {alert['breakout']:.2f} | "
            f"distance {self._format_pct(alert['distance'])} | ATR {self._format_pct(alert['atr_pct'])}"
        )

    def _format_buy_alert(self, alert: Dict[str, Any]) -> str:
        return (
            f"🟢 BUY SETUP {alert['symbol']} | close {alert['close']:.2f} > breakout {alert['breakout']:.2f} | "
            f"stop {alert['stop']:.2f} | target {alert['target']:.2f} | ATR {self._format_pct(alert['atr_pct'])}"
        )

    def _format_exit_alert(self, alert: Dict[str, Any]) -> str:
        return (
            f"🔴 EXIT {alert['symbol']} | {alert['reason']} | close {alert['close']:.2f} | entry {alert['entry']:.2f} | "
            f"PnL {self._format_pct(alert['pnl_pct'])}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trading 212 swing-signal bot")
    parser.add_argument("--config", default="config.example.json", help="Path to JSON config file")
    parser.add_argument("--positions", default="positions.example.json", help="Path to JSON positions file")
    parser.add_argument("--state", default="state.json", help="Path to state file used to suppress duplicate alerts")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    bot = SwingSignalBot(
        config_path=Path(args.config),
        positions_path=Path(args.positions),
        state_path=Path(args.state),
    )
    bot.run()


if __name__ == "__main__":
    main()
