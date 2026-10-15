"""Configuration, candle validation, and deterministic indicator primitives."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone


ENGINE_VERSION = "smc-parente-v1"

H4 = 14_400_000


M15 = 900_000

M5 = 300_000
M30 = 1_800_000



class DataError(ValueError):
    pass


def finite(value, label="number", minimum=None):
    if isinstance(value, bool):
        raise DataError(f"{label} must be numeric, not boolean")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataError(f"Invalid {label}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise DataError(f"Invalid {label}")
    return result


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ms is not None else "—"


def safe_error(exc):
    message = str(exc)
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET"):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
    return message[:350]


@dataclass(frozen=True)
class Rules:
    strategy_model: str = "neural_network"
    nn_model_id: str = "parente_mlp_v1"
    nn_limitations: bool = True
    nn_stop_loss: float = 0.10
    market_mode: str = "spot"
    market_data_source: str = "deribit_first"
    perky_sweep_len: int = 5
    perky_internal_len: int = 5
    perky_swing_len: int = 50
    perky_min_score: int = 4
    perky_htf_agreement: int = 0
    perky_cooldown_bars: int = 25
    perky_score_sweep: bool = True
    perky_score_choch: bool = True
    perky_score_fvg: bool = True
    perky_score_ob: bool = True
    perky_score_killzone: bool = True
    perky_score_ema: bool = True
    perky_score_rsi: bool = True
    perky_score_htf: bool = True
    perky_score_volume: bool = True
    perky_gate_all: bool = True
    perky_killzone_only: bool = False
    perky_require_fvg: bool = False
    perky_require_ob: bool = False
    perky_require_choch: bool = False
    perky_require_smc_bias: bool = False
    perky_ema_filter: bool = False
    perky_rsi_filter: bool = False
    perky_stop_mode: str = "Structure"
    perky_stop_atr_mult: float = 0.5
    perky_structure_sl_buffer_atr: float = 0.25
    smc_setup_minutes: int = 30
    smc_stop_buffer_bps: float = 1.0
    smc_tp_alert_bps: float = 10.0
    smc_min_exit_distance_pct: float = 0.0
    smc_reachability_mode: str = "off"
    smc_reach_bias_minutes: int = 1440
    smc_reach_lookback_bars: int = 60
    smc_reach_swing_len: int = 3
    smc_reach_atr_period: int = 14
    smc_reach_event_bars: int = 80
    smc_reach_entry_zone_pct: float = 50.0
    smc_reach_max_atr: float = 2.0
    smc_reach_balance_bars: int = 3
    smc_reach_balance_atr: float = 1.5
    smc_reach_equal_bps: float = 10.0
    smc_reach_fvg_min_atr: float = 0.1
    smc_reach_fvg_fill_pct: float = 50.0
    smc_reach_require_bias: bool = True
    smc_reach_require_cycle: bool = True
    smc_gex_targets: bool = True
    smc_gex_alignment_bps: float = 50.0
    smc_gex_max_basis_bps: float = 100.0
    max_spread_bps: float = 20.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    paper_equity: float = 1000.0
    paper_floor: float = 950.0
    risk_per_trade: float = 0.005
    max_total_risk: float = 0.02
    max_allocation: float = 0.25
    minimum_notional: float = 10.0

    @property
    def combined(self):
        return True

    @property
    def paper_namespace(self):
        return "neural"

    def validate(self):
        bounds={"perky_sweep_len":(1,50),"perky_internal_len":(1,20),"perky_swing_len":(10,200),
                "perky_min_score":(0,10),"perky_htf_agreement":(0,4),"perky_cooldown_bars":(5,300),
                "smc_reach_lookback_bars":(15,200), "smc_reach_swing_len":(2,20), "smc_reach_atr_period":(1,50),
                "smc_reach_event_bars":(2,600), "smc_reach_balance_bars":(2,20)}
        bool_keys={"nn_limitations","smc_gex_targets","smc_reach_require_bias","smc_reach_require_cycle",
                   "perky_gate_all","perky_killzone_only","perky_require_fvg","perky_require_ob",
                   "perky_require_choch","perky_require_smc_bias","perky_ema_filter","perky_rsi_filter",
                   "perky_score_sweep","perky_score_choch","perky_score_fvg","perky_score_ob",
                   "perky_score_killzone","perky_score_ema","perky_score_rsi","perky_score_htf","perky_score_volume"}
        for key,value in asdict(self).items():
            if key in {"strategy_model","nn_model_id","market_mode","market_data_source","smc_reachability_mode","perky_stop_mode"}:
                allowed={"strategy_model":{"neural_network"},"nn_model_id":{"parente_mlp_v1"},
                         "market_mode":{"spot","margin"},"market_data_source":{"deribit_first","binance"},
                         "perky_stop_mode":{"Structure","ATR"}, "smc_reachability_mode":{"off","advisory","enforce"}}[key]
                if not isinstance(value,str) or value not in allowed:raise DataError("Invalid "+key)
            elif key in bool_keys:
                if type(value) is not bool:raise DataError(key+" must be true or false")
            elif key=="smc_setup_minutes":
                if type(value) is not int or value not in {1,5,15,30,240}:raise DataError("Choose a supported SMC chart timeframe")
            elif key == "smc_reach_bias_minutes":
                if type(value) is not int or value not in {240,1440}:
                    raise DataError("Reachability bias timeframe must be 240 or 1440 minutes")
            elif key in bounds:
                low,high=bounds[key]
                if type(value) is not int or not low<=value<=high:raise DataError(f"{key} must be an integer from {low} to {high}")
            else:
                if type(value) not in {int,float}:raise DataError(key+" must be a JSON number")
                finite(value,key,0)
        for key in ("risk_per_trade","max_total_risk","max_allocation"):
            if not 0<getattr(self,key)<=1:raise DataError(key+" must be in (0, 1]")
        if self.perky_stop_atr_mult < 0.1 or self.perky_stop_atr_mult > 5:
            raise DataError("Perky ATR stop multiplier must be in [0.1, 5]")
        if not 0 <= self.perky_structure_sl_buffer_atr <= 2:
            raise DataError("Perky structure stop buffer must be in [0, 2] ATR")
        if not 0<=self.fee_rate<.05 or not 0<=self.slippage_rate<.05:raise DataError("Fee/slippage must be fractions below 0.05")
        if not 0<self.nn_stop_loss<=.10:raise DataError("Manual paper stop must be in (0, 0.10]")
        if not 0<=self.paper_floor<self.paper_equity:raise DataError("Require 0 <= paper_floor < paper_equity")
        if self.risk_per_trade>self.max_total_risk:raise DataError("Per-trade risk exceeds portfolio risk")
        if not 0<self.smc_stop_buffer_bps<=100:raise DataError("SMC stop buffer must be in (0, 100] bps")
        if not 0<=self.smc_tp_alert_bps<=100:raise DataError("TP proximity must be in [0, 100] bps")
        if not 0 <= self.smc_min_exit_distance_pct <= 100:
            raise DataError("Minimum SMC final-exit distance must be in [0, 100] percent")
        if self.smc_reachability_mode != "off" and self.smc_reach_bias_minutes <= self.smc_setup_minutes:
            raise DataError("Reachability bias timeframe must be higher than the SMC chart timeframe")
        if self.smc_reach_lookback_bars < max(2*self.smc_reach_swing_len+3, self.smc_reach_atr_period+1, self.smc_reach_balance_bars):
            raise DataError("Reachability history must cover swing confirmation, ATR and balance windows")
        if not 0 < self.smc_reach_entry_zone_pct <= 100 or not 0 < self.smc_reach_fvg_fill_pct <= 100:
            raise DataError("Reachability zone and FVG fill percentages must be in (0, 100]")
        if not 0 < self.smc_reach_max_atr <= 20 or not 0 < self.smc_reach_balance_atr <= 20:
            raise DataError("Reachability ATR multiples must be in (0, 20]")
        if not 0 <= self.smc_reach_equal_bps <= 500 or not 0 <= self.smc_reach_fvg_min_atr <= 5:
            raise DataError("Reachability equal-level tolerance or FVG minimum is out of range")
        return self


def exit_distance_pct(entry, target, side):
    """Native-price distance in the trade direction; no currency conversion."""
    if side not in {"long", "short"}:
        raise DataError("Invalid target direction")
    entry = finite(entry, "exit-distance entry", 1e-15)
    target = finite(target, "exit-distance target", 1e-15)
    return (target / entry - 1) * (100 if side == "long" else -100)


DEFAULT_ASSETS = [
    {"name": "BTC", "symbol": "BTC/USDC", "enabled": True, "price_decimals": 2},
    {"name": "ETH", "symbol": "ETH/USDC", "enabled": True, "price_decimals": 2},
    {"name": "SOL", "symbol": "SOL/USDC", "enabled": True, "price_decimals": 3},
]


def normalise_pair(symbol):
    """Canonical explicit exchange pair; compact common quotes are a convenience."""
    if not isinstance(symbol, str):
        raise DataError("Invalid pair; use an explicit BASE/QUOTE such as BTC/USDC")
    value = re.sub(r"\s+", "", symbol.upper())
    explicit = re.fullmatch(r"([^/_-]{1,30})[/_-]([^/_-]{1,30})", value)
    if explicit:
        base, quote = explicit.groups()
        if base != quote and base.isalnum() and quote.isalnum():
            return f"{base}/{quote}"
    elif re.fullmatch(r"[A-Z0-9]{2,60}", value):
        for quote in ("FDUSD", "USDC", "USDT", "TUSD", "BUSD", "USDP", "DAI", "USD", "EUR", "GBP", "AUD", "BRL", "TRY", "JPY", "BTC", "ETH", "BNB"):
            if value.endswith(quote) and len(value) > len(quote):
                base = value[:-len(quote)]
                if len(base) <= 30 and base != quote:
                    return f"{base}/{quote}"
    raise DataError("Invalid pair; use an explicit BASE/QUOTE such as BTC/USDC")


def validate_feed_pair(pair):
    """Validate pair syntax; live exchange catalogs decide availability."""
    return normalise_pair(pair)


def load_settings(path):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else {}
    if not isinstance(document, dict):
        raise DataError("Settings must be a JSON object")
    unknown = set(document) - {"assets", "strategy", "refresh_seconds"}
    if unknown:
        raise DataError(f"Unknown settings: {', '.join(sorted(unknown))}")
    raw_rules = document.get("strategy", {})
    if not isinstance(raw_rules, dict) or set(raw_rules) - set(Rules.__dataclass_fields__):
        raise DataError("Unknown or malformed strategy settings")
    rules = Rules(**raw_rules).validate()
    assets = {}
    pairs = set()
    entries = document.get("assets", DEFAULT_ASSETS)
    if not isinstance(entries, list):
        raise DataError("assets must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("enabled", True)) is not bool:
            raise DataError("Invalid asset entry")
        if not entry.get("enabled", True):
            continue
        name = str(entry.get("name", "")).strip().upper()
        pair = normalise_pair(entry.get("symbol", ""))
        digits = entry.get("price_decimals", 3)
        if not name.isalnum() or name in assets or pair in pairs:
            raise DataError("Asset names and pairs must be distinct")
        if name != pair.split("/")[0]:
            raise DataError(f"{pair}: asset name must match the pair base asset")
        validate_feed_pair(pair)
        if type(digits) is not int or not 0 <= digits <= 10:
            raise DataError("price_decimals must be an integer from 0 to 10")
        assets[name] = {"symbol": pair, "price_decimals": digits}
        pairs.add(pair)
    if not assets:
        raise DataError("Enable at least one asset")
    refresh = finite(document.get("refresh_seconds", 15), "refresh_seconds")
    if not 5 <= refresh <= 300:
        raise DataError("refresh_seconds must be between 5 and 300")
    return assets, rules, refresh



def load_application_settings(path):
    """Run the combined SMC + Parente paper strategy in its neural ledger."""
    assets, rules, refresh = load_settings(path)
    return assets, rules, refresh


@dataclass(frozen=True)
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    interval: int

    @property
    def end(self):
        return self.t + self.interval - 1


def validate_candles(candles, interval, now, minimum=1, fresh=True):
    if len(candles) < minimum:
        raise DataError(f"Need {minimum} completed {interval // 60000}m candles; received {len(candles)}")
    previous = None
    for bar in candles:
        vals = [finite(x, "OHLC price", 1e-15) for x in (bar.o, bar.h, bar.l, bar.c)]
        finite(bar.v, "base volume", 0)
        if bar.h < max(bar.o, bar.c, bar.l) or bar.l > min(bar.o, bar.c):
            raise DataError("Inconsistent OHLC bounds")
        if type(bar.t) is not int or bar.t < 0 or bar.t % interval or bar.interval != interval:
            raise DataError("Wrong candle timestamp units, alignment, or interval")
        if bar.end >= now:
            raise DataError("An uncompleted candle reached the signal engine")
        if previous is not None and bar.t != previous + interval:
            raise DataError("Duplicate, unordered, or missing candles")
        previous = bar.t
    if fresh and now - candles[-1].end > interval + 5000:
        raise DataError("Completed candle feed is stale")
    return candles




def atr_series(candles, period=14):
    result = [None] * len(candles)
    ranges = [max(b.h - b.l, abs(b.h - candles[i-1].c), abs(b.l - candles[i-1].c)) if i else b.h - b.l for i, b in enumerate(candles)]
    if len(ranges) >= period:
        result[period-1] = sum(ranges[:period]) / period
        for i in range(period, len(ranges)):
            result[i] = (result[i-1] * (period-1) + ranges[i]) / period
    return result












def check(key, label, passed=None, measured=None, required=None, candle=None, context=False):
    return {"key": key, "label": label, "status": "context" if context else ("waiting" if passed is None else "pass" if passed else "wait"),
            "measured": measured, "required": required, "candle_ms": candle}


def passed(checks):
    return all(c["status"] in {"pass", "context"} for c in checks)


