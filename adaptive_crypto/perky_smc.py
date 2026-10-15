"""Causal Python strategy port for Perky SMC Ultimate V5 (Perkytradess).

This covers the published script's completed-bar sweep, internal structure,
confluence score, cooldown, and bracket rules. TradingView's drawing layer and
some multi-timeframe inputs are not reproduced; see docs/perky_smc_port.md.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

from .core import Candle, DataError, finite

SOURCE = "perky_smc_v5"
INTERNAL_LEN = 5
SWING_LEN = 50
SWEEP_LEN = 5
MIN_SCORE = 4
COOLDOWN_BARS = 25
ATR_LEN = 14
OB_ATR_LEN = 200
TP_SL_ATR_MULT = 0.5
STRUCTURE_SL_BUFFER_ATR = 0.25


@dataclass(frozen=True)
class PerkySettings:
    sweep_len: int = SWEEP_LEN
    internal_len: int = INTERNAL_LEN
    swing_len: int = SWING_LEN
    min_score: int = MIN_SCORE
    htf_agreement: int = 0
    cooldown_bars: int = COOLDOWN_BARS
    score_sweep: bool = True
    score_choch: bool = True
    score_fvg: bool = True
    score_ob: bool = True
    score_killzone: bool = True
    score_ema: bool = True
    score_rsi: bool = True
    score_htf: bool = True
    score_volume: bool = True
    gate_all: bool = True
    killzone_only: bool = False
    require_fvg: bool = False
    require_ob: bool = False
    require_choch: bool = False
    require_smc_bias: bool = False
    ema_filter: bool = False
    rsi_filter: bool = False
    stop_mode: str = "Structure"
    stop_atr_mult: float = TP_SL_ATR_MULT
    structure_sl_buffer_atr: float = STRUCTURE_SL_BUFFER_ATR


def settings_from_rules(settings=None):
    """Read Perky inputs from Rules, a plain mapping, or PerkySettings."""
    defaults = PerkySettings()
    if settings is None:
        return defaults
    names = {item.name for item in fields(PerkySettings)}
    if isinstance(settings, PerkySettings):
        config = settings
    else:
        values = {}
        for name in names:
            key = "perky_" + name
            if isinstance(settings, dict):
                if key in settings:
                    values[name] = settings[key]
                elif name in settings:
                    values[name] = settings[name]
            elif hasattr(settings, key):
                values[name] = getattr(settings, key)
            elif hasattr(settings, name):
                values[name] = getattr(settings, name)
        if isinstance(settings, dict):
            unknown = set(settings) - names - {"perky_" + name for name in names}
            if unknown:
                raise DataError("Unknown Perky SMC setting: " + ", ".join(sorted(unknown)))
        config = PerkySettings(**(asdict(defaults) | values))
    bounds = {"sweep_len": (1, 50), "internal_len": (1, 20), "swing_len": (2, 200),
              "min_score": (0, 10), "htf_agreement": (0, 4), "cooldown_bars": (5, 300)}
    for name, (low, high) in bounds.items():
        value = getattr(config, name)
        if type(value) is not int or not low <= value <= high:
            raise DataError(f"Perky {name} must be an integer from {low} to {high}")
    for item in fields(config):
        value = getattr(config, item.name)
        if item.name.startswith("score_") or item.name in {"gate_all", "killzone_only", "require_fvg", "require_ob",
                "require_choch", "require_smc_bias", "ema_filter", "rsi_filter"}:
            if type(value) is not bool:
                raise DataError("Perky " + item.name + " must be true or false")
    if config.stop_mode not in {"Structure", "ATR"}:
        raise DataError("Perky stop mode must be Structure or ATR")
    atr_mult = finite(config.stop_atr_mult, "Perky ATR stop multiplier", 0.1)
    buffer = finite(config.structure_sl_buffer_atr, "Perky structure stop buffer", 0)
    if atr_mult > 5 or buffer > 2:
        raise DataError("Perky stop settings are out of range")
    return config


def _get(bar, name):
    if isinstance(bar, dict):
        return bar.get(name)
    return getattr(bar, name, None)


def _bars(candles):
    result = []
    for raw in candles:
        stamp, interval = _get(raw, "t"), _get(raw, "interval")
        if type(stamp) is not int or type(interval) is not int or interval <= 0:
            raise DataError("Invalid Perky SMC candle timestamp")
        result.append(Candle(stamp, finite(_get(raw, "o"), "open", 0),
                             finite(_get(raw, "h"), "high", 0), finite(_get(raw, "l"), "low", 0),
                             finite(_get(raw, "c"), "close", 0), finite(_get(raw, "v"), "volume", 0), interval))
    if not result:
        raise DataError("Waiting for completed Perky SMC candles")
    interval = result[-1].interval
    previous = None
    for bar in result:
        if bar.interval != interval or bar.t < 0 or bar.t % interval:
            raise DataError("Invalid or mixed Perky SMC candle intervals")
        if bar.h < max(bar.o, bar.c) or bar.l > min(bar.o, bar.c) or bar.l > bar.h:
            raise DataError("Inconsistent Perky SMC OHLC bounds")
        if previous is not None and bar.t != previous + interval:
            raise DataError("Duplicate, unordered, or missing Perky SMC candles")
        previous = bar.t
    return result


def _rma(values, length):
    out = [None] * len(values)
    valid = []
    value = None
    for i, item in enumerate(values):
        if item is None:
            continue
        valid.append(item)
        if value is None:
            if len(valid) == length:
                value = sum(valid) / length
        else:
            value = (value * (length - 1) + item) / length
        out[i] = value
    return out


def _ema(values, length):
    alpha = 2 / (length + 1)
    out, value = [], None
    for item in values:
        value = item if value is None else alpha * item + (1 - alpha) * value
        out.append(value)
    return out


def _rsi(bars, length=14):
    gains, losses = [None], [None]
    for i in range(1, len(bars)):
        delta = bars[i].c - bars[i - 1].c
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain, avg_loss = _rma(gains, length), _rma(losses, length)
    out = []
    for gain, loss in zip(avg_gain, avg_loss):
        if gain is None or loss is None:
            out.append(None)
        elif loss == 0:
            out.append(100.0 if gain > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + gain / loss))
    return out


def _atr(bars, length):
    tr = []
    for i, bar in enumerate(bars):
        if i == 0:
            tr.append(bar.h - bar.l)
        else:
            prev = bars[i - 1].c
            tr.append(max(bar.h - bar.l, abs(bar.h - prev), abs(bar.l - prev)))
    return _rma(tr, length)


def _pivot(bars, index, key, length, low):
    if index < length * 2:
        return None
    center = index - length
    value = getattr(bars[center], key)
    for offset in range(1, length + 1):
        older, newer = getattr(bars[center - offset], key), getattr(bars[center + offset], key)
        if (older < value or newer <= value) if low else (older > value or newer >= value):
            return None
    return {"price": value, "bar": center, "bar_ms": bars[center].t}


def _htf_bias_series(bars):
    """Causal approximation of the script's 15m/1h/4h/D EMA20/50 dashboard."""
    interval = bars[-1].interval
    result = [{} for _ in bars]
    for tf in (900_000, 3_600_000, 14_400_000, 86_400_000):
        if tf < interval or tf % interval:
            continue
        groups = {}
        for bar in bars:
            bucket = bar.t // tf
            entry = groups.setdefault(bucket, {"first": bar.t, "last": bar.end, "o": bar.o,
                                                "h": bar.h, "l": bar.l, "c": bar.c, "v": bar.v,
                                                "n": 0})
            entry["last"], entry["h"], entry["l"], entry["c"] = bar.end, max(entry["h"], bar.h), min(entry["l"], bar.l), bar.c
            entry["v"] += bar.v
            entry["n"] += 1
        aggregated = []
        for bucket, row in sorted(groups.items()):
            start, end = bucket * tf, (bucket + 1) * tf - 1
            if row["first"] != start or row["last"] != end or row["n"] * interval != tf:
                continue
            aggregated.append(Candle(start, row["o"], row["h"], row["l"], row["c"], row["v"], tf))
        if not aggregated:
            continue
        fast, slow = _ema([item.c for item in aggregated], 20), _ema([item.c for item in aggregated], 50)
        position = -1
        current_bias = None
        for i, bar in enumerate(bars):
            while position + 1 < len(aggregated) and aggregated[position + 1].end <= bar.end:
                position += 1
                current_bias = 1 if fast[position] > slow[position] else -1 if fast[position] < slow[position] else 0
            if current_bias is not None:
                result[i][tf] = current_bias
    return result


class _PerkyEngine:
    def __init__(self, bars, settings):
        self.bars = bars
        self.settings = settings
        self.interval = bars[-1].interval
        self.atr = _atr(bars, ATR_LEN)
        self.atr200 = _atr(bars, OB_ATR_LEN)
        self.parsed_highs, self.parsed_lows = [], []
        for bar, atr in zip(bars, self.atr200):
            if atr is not None and bar.h - bar.l >= 2 * atr:
                self.parsed_highs.append(bar.l)
                self.parsed_lows.append(bar.h)
            else:
                self.parsed_highs.append(bar.h)
                self.parsed_lows.append(bar.l)
        self.htf_bias = _htf_bias_series(bars)
        self.rsi = _rsi(bars)
        closes = [bar.c for bar in bars]
        fast_len, slow_len = ((9, 21) if self.interval < 300_000 else (50, 200))
        self.ema_fast, self.ema_slow = _ema(closes, fast_len), _ema(closes, slow_len)
        self.internal = self._new_structure_state()
        self.swing = self._new_structure_state()
        self.sweep_highs, self.sweep_lows = [], []
        self.swing_points = []
        self.fvgs, self.order_blocks, self.events = [], [], []
        self.last_bull_choch = self.last_bear_choch = False
        self.last_entry = -10**9
        self.last_long_entry = self.last_short_entry = -10**9
        self.cumulative_gap_body = 0.0
        self.latest = None

    @staticmethod
    def _new_structure_state():
        return {"leg": 0, "high": None, "low": None, "bias": 0}

    def _update_structure(self, index, size, state, internal):
        if index < size:
            return []
        candidate = index - size
        highs = [bar.h for bar in self.bars[index - size + 1:index + 1]]
        lows = [bar.l for bar in self.bars[index - size + 1:index + 1]]
        new_leg = 0 if self.bars[candidate].h > max(highs) else 1 if self.bars[candidate].l < min(lows) else state["leg"]
        changed = new_leg != state["leg"]
        if changed:
            state["leg"] = new_leg
            key = "low" if new_leg == 1 else "high"
            state[key] = {"price": getattr(self.bars[candidate], "l" if key == "low" else "h"),
                          "bar": candidate, "bar_ms": self.bars[candidate].t,
                          "known_ms": self.bars[index].end, "known_index": index, "crossed": False}
            if not internal:
                self.swing_points.append({"price": state[key]["price"],
                    "bar_ms": state[key]["bar_ms"], "known_ms": self.bars[index].end,
                    "known_index": index, "side": "long" if key == "high" else "short"})
        bar = self.bars[index]
        previous_close = self.bars[index - 1].c if index else bar.c
        found = []
        for key, direction in (("high", 1), ("low", -1)):
            pivot = state[key]
            if pivot is None or pivot["crossed"]:
                continue
            if internal and pivot["price"] == (self.swing[key]["price"] if self.swing[key] else None):
                continue
            broke = (previous_close <= pivot["price"] < bar.c) if direction == 1 else (previous_close >= pivot["price"] > bar.c)
            if not broke:
                continue
            kind = "CHoCH" if state["bias"] == -direction else "BOS"
            pivot["crossed"] = True
            state["bias"] = direction
            event = {"kind": kind, "side": "long" if direction == 1 else "short",
                     "level": pivot["price"], "pivot_ms": pivot["bar_ms"],
                     "time_ms": bar.t, "known_ms": bar.end, "strength": None,
                     "structure": "internal" if internal else "swing"}
            found.append(event)
            if internal:
                self._store_order_block(index, pivot, direction)
        return found

    def _store_order_block(self, index, pivot, direction):
        start = max(0, pivot["bar"])
        indices = range(start, index)
        if start >= index:
            return
        source_index = min(indices, key=lambda i: self.parsed_lows[i]) if direction == 1 else max(indices, key=lambda i: self.parsed_highs[i])
        self.order_blocks.insert(0, {"bias": direction, "low": self.parsed_lows[source_index],
                                     "high": self.parsed_highs[source_index],
                                     "bar_ms": self.bars[source_index].t, "known_ms": self.bars[index].end})
        del self.order_blocks[100:]

    def _update_sweeps(self, index):
        bar = self.bars[index]
        for source, key, low in ((self.sweep_highs, "h", False), (self.sweep_lows, "l", True)):
            point = _pivot(self.bars, index, key, self.settings.sweep_len, low)
            if point:
                point.update(known_ms=bar.end, known_index=index, broken=False, wick_seen=False, mitigated=False)
                source.insert(0, point)
        fired = []
        for point in self.sweep_highs:
            if point["mitigated"]:
                continue
            if not point["broken"]:
                if bar.c > point["price"]:
                    point["mitigated"] = True
                elif not point["wick_seen"] and bar.h > point["price"] and bar.c < point["price"]:
                    point["wick_seen"] = True
                    fired.append({"kind": "SWEEP", "side": "short", "level": point["price"],
                                  "signal_price": bar.h, "pivot_ms": point["bar_ms"]})
        for point in self.sweep_lows:
            if point["mitigated"]:
                continue
            if not point["broken"]:
                if bar.c < point["price"]:
                    point["mitigated"] = True
                elif not point["wick_seen"] and bar.l < point["price"] and bar.c > point["price"]:
                    point["wick_seen"] = True
                    fired.append({"kind": "SWEEP", "side": "long", "level": point["price"],
                                  "signal_price": bar.l, "pivot_ms": point["bar_ms"]})
        self.sweep_highs = [point for point in self.sweep_highs if not point["mitigated"] and index - point["bar"] <= 2000]
        self.sweep_lows = [point for point in self.sweep_lows if not point["mitigated"] and index - point["bar"] <= 2000]
        return fired

    def _update_fvgs(self, index):
        bar = self.bars[index]
        self.fvgs = [gap for gap in self.fvgs if not
                     (bar.l < gap["bottom"] if gap["bias"] == 1 else bar.h > gap["top"])]
        if index >= 2:
            previous = self.bars[index - 1]
            delta = (previous.c - previous.o) / (previous.o * 100) if previous.o else 0.0
            self.cumulative_gap_body += abs(delta)
            threshold = self.cumulative_gap_body / index * 2 if index > 0 else 0.0
            if bar.l > self.bars[index - 2].h and previous.c > self.bars[index - 2].h and delta > threshold:
                self.fvgs.insert(0, {"bias": 1, "top": bar.l, "bottom": self.bars[index - 2].h,
                                     "known_ms": bar.end})
            if bar.h < self.bars[index - 2].l and previous.c < self.bars[index - 2].l and -delta > threshold:
                self.fvgs.insert(0, {"bias": -1, "top": self.bars[index - 2].l, "bottom": bar.h,
                                     "known_ms": bar.end})
        del self.fvgs[100:]

    def _update_order_blocks(self, index):
        bar = self.bars[index]
        kept = []
        for block in self.order_blocks:
            crossed = bar.h > block["high"] if block["bias"] == -1 else bar.l < block["low"]
            if not crossed:
                kept.append(block)
        self.order_blocks = kept

    def _score(self, side, sweep, atr200, htf, index):
        bar = self.bars[index]
        direction = 1 if side == "long" else -1
        in_fvg = any(gap["bias"] == direction and gap["bottom"] <= bar.c <= gap["top"] for gap in self.fvgs)
        near_ob = False
        if atr200 is not None:
            near_ob = any(block["bias"] == direction and
                          (block["low"] <= bar.c <= block["high"] + atr200 * 2 if direction == 1
                           else block["low"] - atr200 * 2 <= bar.c <= block["high"])
                          for block in self.order_blocks)
        minute = datetime.fromtimestamp(bar.t / 1000, timezone.utc).hour * 60 + datetime.fromtimestamp(bar.t / 1000, timezone.utc).minute
        in_kz = 8 * 60 <= minute < 10 * 60 or 13 * 60 + 30 <= minute < 15 * 60 + 30
        fast = self.ema_fast[index]
        above_ema = bar.c > fast
        rsi = self.rsi[index]
        rsi_ok = rsi is not None and (rsi < 70 if direction == 1 else rsi > 30)
        htf_count = sum(value == direction for value in htf.values())
        volume_window = self.bars[max(0, index - 19):index + 1]
        volume_average = sum(item.v for item in volume_window) / 20 if len(volume_window) == 20 else None
        volume_ok = volume_average is not None and bar.v > volume_average
        choch = self.last_bull_choch if direction == 1 else self.last_bear_choch
        features = {"liquidity_sweep": bool(sweep), "choch_confirmed": bool(choch),
                    "fvg_in_direction": bool(in_fvg), "order_block_in_direction": bool(near_ob),
                    "kill_zone": bool(in_kz), "ema_trend": bool(above_ema if direction == 1 else bar.c < fast),
                    "rsi_not_exhausted": bool(rsi_ok), "htf_bias": htf_count >= 2,
                    "volume_above_average": bool(volume_ok)}
        weights = {"liquidity_sweep": (2, self.settings.score_sweep),
                   "choch_confirmed": (1, self.settings.score_choch),
                   "fvg_in_direction": (1, self.settings.score_fvg),
                   "order_block_in_direction": (1, self.settings.score_ob),
                   "kill_zone": (1, self.settings.score_killzone),
                   "ema_trend": (1, self.settings.score_ema),
                   "rsi_not_exhausted": (1, self.settings.score_rsi),
                   "htf_bias": (1, self.settings.score_htf),
                   "volume_above_average": (1, self.settings.score_volume)}
        parts = {name: weight if features[name] and enabled else 0
                 for name, (weight, enabled) in weights.items()}
        return sum(parts.values()), parts, {"count": htf_count, "available": len(htf)}, features

    def _targets(self, side, atr, index):
        bar = self.bars[index]
        long = side == "long"
        if long:
            fvg = [gap["bottom"] for gap in self.fvgs if gap["bias"] == 1 and gap["bottom"] > bar.c]
            ob = [block["low"] for block in self.order_blocks if block["bias"] == -1 and block["low"] > bar.c]
            swing = self.swing["high"]["price"] if self.swing["high"] else None
            targets = [min(fvg) if fvg else bar.c + atr,
                       min(ob) if ob else bar.c + atr * 1.5,
                       swing if swing is not None else bar.c + atr * 2]
            return sorted(targets)
        fvg = [gap["top"] for gap in self.fvgs if gap["bias"] == -1 and gap["top"] < bar.c]
        ob = [block["high"] for block in self.order_blocks if block["bias"] == 1 and block["high"] < bar.c]
        swing = self.swing["low"]["price"] if self.swing["low"] else None
        targets = [max(fvg) if fvg else bar.c - atr,
                   max(ob) if ob else bar.c - atr * 1.5,
                   swing if swing is not None else bar.c - atr * 2]
        return sorted(targets, reverse=True)

    def _stop(self, side, signal_price, entry_price, atr, index):
        if self.settings.stop_mode == "ATR":
            distance = atr * self.settings.stop_atr_mult
            return entry_price - distance if side == "long" else entry_price + distance
        long = side == "long"
        bar = self.bars[index]
        anchor = min(self.swing["low"]["price"], bar.l) if long and self.swing["low"] else bar.l
        if not long:
            anchor = max(self.swing["high"]["price"], bar.h) if self.swing["high"] else bar.h
        if long:
            structural = min(max(anchor - atr * self.settings.structure_sl_buffer_atr, signal_price - atr * 3), signal_price - atr * 0.15)
            return structural
        structural = max(min(anchor + atr * self.settings.structure_sl_buffer_atr, signal_price + atr * 3), signal_price + atr * 0.15)
        return structural

    def feed(self, index):
        bar = self.bars[index]
        self._last_candidates = []
        self._last_fired = []
        self.latest = None
        sweeps = self._update_sweeps(index)
        events = self._update_structure(index, self.settings.swing_len, self.swing, False)
        internal_events = self._update_structure(index, self.settings.internal_len, self.internal, True)
        events.extend(internal_events)
        for event in events:
            self.events.append(event)
            if event["structure"] == "internal" and event["kind"] == "CHoCH":
                self.last_bull_choch = event["side"] == "long"
                self.last_bear_choch = event["side"] == "short"
        self._update_order_blocks(index)
        self._update_fvgs(index)
        atr = self.atr[index]
        if atr is None or atr <= 0:
            self.latest = None
            return
        htf = self.htf_bias[index]
        raw = []
        if sweeps:
            raw.extend(sweeps)
        for event in internal_events:
            if event["kind"] == "CHoCH":
                raw.append({**event, "signal_price": bar.l if event["side"] == "long" else bar.h})
        if index >= 2:
            bull_momentum = bar.c > bar.o and self.bars[index - 1].c < self.bars[index - 1].o and self.bars[index - 2].c < self.bars[index - 2].o
            bear_momentum = bar.c < bar.o and self.bars[index - 1].c > self.bars[index - 1].o and self.bars[index - 2].c > self.bars[index - 2].o
            if bull_momentum:
                raw.append({"kind": "MOMENTUM", "side": "long", "signal_price": bar.l})
            if bear_momentum:
                raw.append({"kind": "MOMENTUM", "side": "short", "signal_price": bar.h})
        recent = self.bars[max(0, index - 10):index]
        expansion_up = bool(recent and bar.c > max(item.h for item in recent))
        expansion_down = bool(recent and bar.c < min(item.l for item in recent))
        shared_allowed = index - self.last_entry >= self.settings.cooldown_bars or (index - self.last_entry >= 5 and (expansion_up or expansion_down))
        fired = []
        # Pine's entry priority is sweep, then CHoCH, then momentum for each side.
        for side in ("long", "short"):
            group = [item for item in raw if item["side"] == side]
            if not group:
                continue
            group.sort(key=lambda item: {"SWEEP": 0, "CHoCH": 1, "MOMENTUM": 2}.get(item["kind"], 3))
            score, parts, htf_evidence, features = self._score(side, any(item["kind"] == "SWEEP" for item in group), self.atr200[index], htf, index)
            qualifying = (score >= self.settings.min_score and htf_evidence["count"] >= self.settings.htf_agreement
                          and (not self.settings.killzone_only or features["kill_zone"]))
            filter_ok = ((not self.settings.require_fvg or features["fvg_in_direction"])
                         and (not self.settings.require_ob or features["order_block_in_direction"])
                         and (not self.settings.require_choch or features["choch_confirmed"])
                         and (not self.settings.require_smc_bias or self.internal["bias"] == (1 if side == "long" else -1))
                         and (not self.settings.ema_filter or features["ema_trend"])
                         and (not self.settings.rsi_filter or features["rsi_not_exhausted"]))
            candidate = None
            candidate_cooldown_allowed = False
            for option in group:
                if option["kind"] == "SWEEP":
                    allowed = shared_allowed and qualifying and filter_ok
                    cooldown_allowed = shared_allowed
                else:
                    side_last = self.last_long_entry if side == "long" else self.last_short_entry
                    side_allowed = (index - side_last >= self.settings.cooldown_bars or
                                    (index - side_last >= 5 and (expansion_up or expansion_down)))
                    cooldown_allowed = side_allowed
                    requires_gate = option["kind"] == "CHoCH" or self.settings.gate_all
                    allowed = (not requires_gate or qualifying) and (not self.settings.gate_all or filter_ok)
                    if self.settings.gate_all:
                        allowed = allowed and side_allowed
                    if option["kind"] == "MOMENTUM":
                        allowed = allowed and not any(item["kind"] == "SWEEP" for item in group) and not any(
                            event["kind"] == "CHoCH" and event["side"] == side for event in internal_events)
                if allowed:
                    candidate = option
                    candidate_cooldown_allowed = cooldown_allowed
                    break
            if candidate is None:
                candidate = group[0]
                if candidate["kind"] == "SWEEP":
                    candidate_cooldown_allowed = shared_allowed
                else:
                    side_last = self.last_long_entry if side == "long" else self.last_short_entry
                    candidate_cooldown_allowed = (index - side_last >= self.settings.cooldown_bars or
                                                  (index - side_last >= 5 and (expansion_up or expansion_down)))
                allowed = False
            signal = {"kind": candidate["kind"], "side": side, "level": candidate.get("level"),
                      "signal_price": candidate["signal_price"], "signal_ms": bar.t, "signal_end": bar.end,
                      "known_ms": bar.end, "score": score, "minimum_score": self.settings.min_score,
                      "score_parts": parts, "htf_agreement": htf_evidence,
                      "score_features": features,
                      "qualifies": bool(allowed), "atr": atr, "cooldown_allowed": bool(candidate_cooldown_allowed),
                      "bullish_bias": self.internal["bias"] == 1, "bearish_bias": self.internal["bias"] == -1}
            if allowed:
                signal["targets"] = self._targets(side, atr, index)
                signal["stop"] = self._stop(side, candidate["signal_price"], candidate["signal_price"], atr, index)
                self.last_entry = index
                if side == "long":
                    self.last_long_entry = index
                else:
                    self.last_short_entry = index
                fired.append(signal)
            self._last_candidates.append(signal)
            self.latest = signal
        if not raw:
            self.latest = None
        for event in sweeps:
            self.events.append({**event, "known_ms": bar.end, "time_ms": bar.t, "strength": None,
                                "structure": "sweep"})
        # Retain current-bar events and the most recent prior signal for context.
        self._last_fired = fired

    def snapshot(self):
        last = self.bars[-1]
        settings = asdict(self.settings) | {"mode": "Filtered"}
        strategy = {"source": SOURCE, "script_title": "Perky SMC Ultimate V5", "status": "ready",
                    "asof_ms": last.end, "interval_ms": self.interval, "last_bar_ms": last.t,
                    "last_closed_ms": last.end, "close": last.c,
                    "regime": {1: "bullish", -1: "bearish", 0: "neutral"}[self.internal["bias"]],
                    "internal_bias": self.internal["bias"],
                    "swing_high": self.swing["high"]["price"] if self.swing["high"] else None,
                    "swing_low": self.swing["low"]["price"] if self.swing["low"] else None,
                    "events": [dict(event) for event in self.events],
                    "signal": copy_signal(self.latest),
                    "signals": [copy_signal(item) for item in getattr(self, "_last_candidates", [])],
                    "qualified_signals": [copy_signal(item) for item in getattr(self, "_last_fired", [])],
                    "settings": settings}
        liquidity = []
        for point in self.swing_points:
            later = next((bar for bar in self.bars[point["known_index"]+1:]
                          if (bar.h >= point["price"] if point["side"] == "long" else bar.l <= point["price"])), None)
            liquidity.append({k: point[k] for k in ("side", "price", "bar_ms", "known_ms")} |
                             {"taken": later is not None, "taken_ms": later.end if later else None})
        zones = [{"id": f"ob:{block['known_ms']}:{block['bias']}",
                  "kind": "OB", "side": "long" if block["bias"] == 1 else "short",
                  "low": block["low"], "high": block["high"], "created_ms": block["bar_ms"],
                  "known_ms": block["known_ms"], "status": "active", "score": 1, "touches": 0}
                 for block in self.order_blocks]
        zones.extend({"id": f"fvg:{gap['known_ms']}:{gap['bias']}", "kind": "FVG",
                      "side": "long" if gap["bias"] == 1 else "short", "low": gap["bottom"],
                      "high": gap["top"], "created_ms": gap["known_ms"],
                      "known_ms": gap["known_ms"], "status": "active", "score": 1, "touches": 0}
                     for gap in self.fvgs)
        current_signal = self.latest
        quality = {"source": SOURCE, "score": current_signal.get("score") if current_signal else None,
                   "score_parts": dict(current_signal.get("score_parts", {})) if current_signal else {},
                   "minimum_score": self.settings.min_score, "mode": "Filtered",
                   "note": "Perky SMC confluence score, not a probability of profit."}
        return {**strategy, "source": "pine_smc", "perky": strategy,
                "liquidity": liquidity, "zones": zones,
                "quality": quality, "structure_quality": quality}


def copy_signal(signal):
    if signal is None:
        return None
    return {key: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
            for key, value in signal.items()}


def run_perky_strategy(candles, now=None, settings=None):
    bars = _bars(candles)
    if now is not None:
        if type(now) is not int or now < 0:
            raise DataError("Invalid Perky SMC observation time")
        bars = [bar for bar in bars if bar.end < now]
        if not bars:
            raise DataError("Waiting for a completed Perky SMC candle")
    engine = _PerkyEngine(bars, settings_from_rules(settings))
    for index in range(len(bars)):
        engine.feed(index)
    return engine.snapshot()


__all__ = ["PerkySettings", "run_perky_strategy", "settings_from_rules"]
