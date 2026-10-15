"""Shared Parente classification and SMC-confirmed, idempotent paper execution."""
from __future__ import annotations

import copy
import math

from .core import DataError, H4, M5, exit_distance_pct, finite, passed, safe_error, validate_candles
from .neural import NeuralModel
from .neural_input_contract import (PARENTE_BINANCE_USDT, input_cache_identity,
                                    normalise_input_source)
from .neural_ledger import close_trade, open_combined_trade, _account_fx
from .take_profit_alerts import fresh_quote


class NeuralEngine:
    def __init__(self, assets, rules, store, model=None, model_path=None, settings_path=None,
                 feature_provider=None, candle_source=None):
        self.assets, self.rules, self.store = assets, rules, store
        self.feature_provider = feature_provider
        self.candle_source = copy.deepcopy(candle_source)
        self.model, self.model_error, self.cache = model, None, {}
        if model is None:
            try:
                if rules.nn_model_id != "parente_mlp_v1":
                    raise DataError("Only the trained Parente 5/2 model is supported")
                self.model = NeuralModel(model_path or None)
            except Exception as exc:
                # Saved SMC stops and targets remain monitored during model outages.
                self.model_error = safe_error(exc)

    @property
    def decision_interval_ms(self):
        return H4

    def paper_signal(self, name, signal, document):
        return copy.deepcopy(signal)

    def holding_reading(self, reading, position):
        return copy.deepcopy(reading)

    def _authenticate(self, name, reading, now):
        """Verify the loaded model, completed candle and actual input contract."""
        if not isinstance(reading, dict):
            return None, "Neural reading is malformed"
        signal = reading.get("signal")
        if reading.get("error"):
            return None, reading["error"]
        if not isinstance(signal, dict):
            return None, "Waiting for the latest completed Parente 4H signal"
        identity = getattr(self.model, "identity", None)
        if not identity:
            return None, "No trained Parente model is loaded"
        if signal.get("model_id") != identity or reading.get("model_id") != identity:
            return None, "Neural signal does not match the loaded Parente model"
        probabilities = signal.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != {"BUY", "HOLD", "SELL"}:
            return None, "Neural probabilities are missing or malformed"
        values = {}
        for cls in ("BUY", "HOLD", "SELL"):
            try:
                if isinstance(probabilities[cls], bool):
                    raise ValueError("Boolean probability")
                value = float(probabilities[cls])
            except (TypeError, ValueError, OverflowError):
                return None, "Neural probability is not numeric"
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                return None, "Neural probability is not finite or within [0, 1]"
            values[cls] = value
        if abs(sum(values.values())-1.0) > 1e-6:
            return None, "Neural probabilities are not normalised"
        if signal.get("label") != max(values, key=values.get):
            return None, "Neural label does not match the probability argmax"
        end = signal.get("signal_end")
        if type(end) is not int or end != (now//H4)*H4-1:
            return None, "Waiting for the latest completed Parente 4H signal"
        if end <= getattr(self.model, "trained_through_ms", 0):
            return None, "Live signal must follow the model training/calibration period"
        if type(reading.get("expires_ms")) is not int or reading["expires_ms"] != end+H4+1:
            return None, "Neural signal expiry is invalid"
        validated = copy.deepcopy(signal)
        validated["probabilities"] = values
        source = reading.get("input_source")
        require_source = isinstance(self.model, NeuralModel) or self.feature_provider is not None
        try:
            if source is not None or require_source:
                source = normalise_input_source(source, interval_ms=H4)
                if require_source:
                    asset = self.assets[name]["symbol"].split("/")[0]
                    PARENTE_BINANCE_USDT.validate(source, asset)
            nested = signal.get("input_source")
            if nested is not None:
                nested = normalise_input_source(nested, interval_ms=H4)
                if not self._source_same(source, nested):
                    raise DataError("Neural signal provenance differs from its input reading")
            if source is not None:
                validated["input_source"] = copy.deepcopy(source)
        except (DataError, KeyError, TypeError, AttributeError, ValueError) as exc:
            return None, safe_error(exc)
        return validated, None

    @staticmethod
    def _source_same(a, b):
        if a is None or b is None:
            return a is None and b is None
        try:
            a = normalise_input_source(a, interval_ms=H4)
            b = normalise_input_source(b, interval_ms=H4)
            return (a["venue"].casefold(), a["market"], a["pair"], a["interval_ms"], a["volume"]) == (
                b["venue"].casefold(), b["market"], b["pair"], b["interval_ms"], b["volume"])
        except (DataError, KeyError, TypeError, AttributeError, ValueError):
            return False

    @staticmethod
    def _baseline_conflict(baseline, signal):
        """Reject a same-candle, same-model, same-source label/probability flip."""
        if not isinstance(baseline, dict):
            return False
        if baseline.get("signal_end") != signal.get("signal_end"):
            return False
        if baseline.get("model_id") != signal.get("model_id"):
            return False
        if not NeuralEngine._source_same(baseline.get("input_source"), signal.get("input_source")):
            return False
        if baseline.get("label") != signal.get("label"):
            return True
        base_probs = baseline.get("probabilities")
        new_probs = signal.get("probabilities")
        if not isinstance(base_probs, dict) or not isinstance(new_probs, dict):
            return True
        if set(base_probs) != {"BUY", "HOLD", "SELL"} or set(new_probs) != {"BUY", "HOLD", "SELL"}:
            return True
        for cls in ("BUY", "HOLD", "SELL"):
            try:
                if abs(float(base_probs[cls]) - float(new_probs[cls])) > 1e-9:
                    return True
            except (TypeError, ValueError, KeyError, OverflowError):
                return True
        return False

    def read(self, name, high, now, errors=None):
        """Cache the fixed Binance/USDT 4H classification for every consumer."""
        errors = errors or {}
        signal, problem, latest, input_source = None, self.model_error, None, None
        if "clock" in errors and self.feature_provider is None:
            problem = "Exchange clock unavailable"
        else:
            try:
                symbol = self.assets[name]["symbol"]
                asset = symbol.split("/")[0]
                if self.feature_provider is not None:
                    high, input_source = self.feature_provider.candles(asset, now)
                    input_source = normalise_input_source(input_source, interval_ms=H4)
                    PARENTE_BINANCE_USDT.validate(input_source, asset)
                elif self.candle_source is not None:
                    input_source = normalise_input_source(self.candle_source, pair=symbol, interval_ms=H4)
                    if isinstance(self.model, NeuralModel):
                        PARENTE_BINANCE_USDT.validate(input_source, asset)
                elif isinstance(self.model, NeuralModel):
                    raise DataError("Parente requires its Binance spot/USDT 4H input feed")
                validate_source = getattr(type(self.model), "validate_input_source", None)
                if callable(validate_source):
                    validate_source(self.model, input_source, asset)
                validate_candles(high, H4, now, minimum=getattr(self.model, "required_history_bars", 100))
                if high[-1].t != (now//H4-1)*H4:
                    raise DataError("Latest completed 4H candle is missing")
                latest = high[-1].t
                if self.model is not None:
                    if high[-1].t <= self.model.trained_through_ms:
                        raise DataError("Live signal must follow the model training/calibration period")
                    source_key = input_cache_identity(input_source, pair=symbol, interval_ms=H4)
                    key = (name, self.model.identity, source_key, high[-1].end)
                    if key not in self.cache:
                        predicted = self.model.predict(high, asset)
                        self.cache = {k: v for k, v in self.cache.items() if k[0] != name}
                        self.cache[key] = predicted
                    signal = copy.deepcopy(self.cache[key])
                    if input_source is not None:
                        signal["input_source"] = copy.deepcopy(input_source)
            except Exception as exc:
                signal, problem = None, safe_error(exc)
        reading = {"signal": copy.deepcopy(signal), "error": problem,
                   "expires_ms": signal["signal_end"]+H4+1 if isinstance(signal, dict) and type(signal.get("signal_end")) is int else now,
                   "model_id": self.model.identity if self.model else None,
                   "input_source": copy.deepcopy(input_source), "latest_4h_ms": latest,
                   "latest_decision_ms": latest}
        signal, problem = self._authenticate(name, reading, now)
        if signal is not None:
            baseline = self.store.snapshot()["assets"][name].get("last_signal") if self.store is not None else None
            if self._baseline_conflict(baseline, signal):
                signal, problem = None, "Rejected conflicting Parente signal for the same completed candle and input"
        reading.update(signal=copy.deepcopy(signal), error=problem)
        return reading

    @staticmethod
    def _qualified_plan(market, quote, now, minimum_exit_distance_pct=0.0, reachability_rules=None):
        """Recheck the exact qualified Perky proposal at the execution boundary."""
        if not isinstance(market, dict) or market.get("source") != "pine_smc":
            raise DataError("Waiting for a qualified Perky SMC entry")
        guide = market.get("guide") or {}
        if guide.get("status") != "qualified" or guide.get("expires_ms", 0) <= now:
            raise DataError("Waiting for a fresh qualified Perky SMC entry")
        choices = [p for p in market.get("suggestions", []) if p.get("side") == "long"]
        proposed = market.get("suggestion")
        if isinstance(proposed, dict) and proposed.get("side") == "long":
            choices.insert(0, proposed)
        if not choices:
            raise DataError("Waiting for a qualified long Perky SMC entry")
        reasons = []
        for plan in choices:
            try:
                if (plan.get("source") != "pine_smc" or plan.get("side") != "long"
                        or not isinstance(plan.get("id", plan.get("key")), str)
                        or not plan.get("id", plan.get("key"))
                        or type(plan.get("signal_end")) is not int
                        or not 0 <= plan["signal_end"] < now
                        or type(plan.get("expires_ms")) is not int or plan["expires_ms"] <= now
                        or not plan.get("checks") or not passed(plan["checks"])):
                    raise DataError("SMC entry evidence is missing, expired or unqualified")
                if quote["asof_ms"] <= plan["signal_end"]:
                    raise DataError("Waiting for a quote after the SMC confirmation")
                stop = finite(plan.get("stop"), "SMC stop", 1e-15)
                tp1 = finite(plan.get("tp1", plan.get("target")), "SMC TP1", 1e-15)
                target = finite(plan.get("exit_price", plan.get("target")), "SMC execution target", 1e-15)
                if not stop < quote["bid"] <= quote["ask"] < tp1 <= target:
                    raise DataError("Actual executable quote is outside the SMC stop/target bracket")
                if exit_distance_pct(quote["ask"], target, "long") + 1e-9 < minimum_exit_distance_pct:
                    raise DataError("Current SMC exit is below the minimum target distance")
                if reachability_rules is not None:
                    from .smc_reachability import require_target
                    require_target(plan, quote["ask"], reachability_rules, now, market.get("symbol"))
                return copy.deepcopy(plan)
            except (DataError, KeyError, TypeError) as exc:
                reasons.append(str(exc))
        raise DataError(reasons[0])

    def evaluate(self, name, quote, high, low, now, errors=None, *, smc_market=None,
                 neural_reading=None, fx_quote=None):
        errors = dict(errors or {})
        observed_quote = quote
        quote = fresh_quote(quote, now) if "clock" not in errors else None
        display_quote = dict(quote) if quote else None
        if display_quote is not None:
            try:
                display_quote["last"] = finite(observed_quote["last"], "last trade", 1e-15)
            except (DataError, KeyError, TypeError):
                pass
        row = {"name": name, "symbol": self.assets[name]["symbol"], "asof_ms": now,
               "quote": display_quote, "errors": errors,
               "neural": {"status": "WAITING", "signal": None}}
        valid_low = []
        if "clock" not in errors:
            try:
                valid_low = validate_candles(low, M5, now, fresh=True)
                if valid_low[-1].t != (now//M5-1)*M5:
                    raise DataError("Latest completed stop/target-monitoring candle is missing")
            except (DataError, TypeError, AttributeError) as exc:
                valid_low = []
                errors["stop_history"] = safe_error(exc)
        reading = copy.deepcopy(neural_reading) if neural_reading is not None else self.read(name, high, now, errors)
        if not isinstance(reading, dict):
            reading = {"signal": None, "error": "Neural reading is malformed"}
        signal, problem = self._authenticate(name, reading, now)
        row["neural"].update(reading)
        row["neural"]["signal"] = copy.deepcopy(signal)
        row["neural"]["error"] = problem
        if reading.get("latest_4h_ms") is not None:
            row["latest_4h_ms"] = reading["latest_4h_ms"]

        def update(document):
            execution_now, execution_quote = now, quote
            historical_clock_ok = "clock" not in errors
            clock = getattr(self, "clock", None)
            if self.feature_provider is not None and callable(clock):
                try:
                    execution_now = clock()
                    if type(execution_now) is not int or execution_now < now:
                        raise DataError("Exchange clock moved backwards during the neural read")
                    execution_quote = fresh_quote(quote, execution_now) if quote else None
                except Exception as exc:
                    execution_quote = None
                    errors["clock"] = safe_error(exc)
            row["asof_ms"] = execution_now
            if execution_quote is None:
                row["quote"] = None
            record = document["assets"][name]
            try:
                execution_fx = _account_fx(self.assets[name]["symbol"], fx_quote, execution_now)
            except DataError as exc:
                errors["paper_fx"] = errors.get("paper_fx") or safe_error(exc)
                record["last_result"] = "Waiting for a fresh USD conversion: " + errors["paper_fx"]
                return
            for trade in record["trades"]:
                if trade["status"] != "active":
                    continue
                stop, target = trade.get("stop"), trade.get("target")
                if stop is not None or target is not None:
                    if not valid_low or not historical_clock_ok:
                        trade["tracking_gap"] = True
                    if historical_clock_ok:
                        for bar in valid_low:
                            if bar.end <= trade["last_bar_end"] or bar.t < trade["opened_ms"]:
                                continue
                            expected_start = (((trade["opened_ms"]+M5-1)//M5)*M5
                                              if trade["last_bar_end"] == trade["opened_ms"]
                                              else trade["last_bar_end"]+1)
                            if bar.t > expected_start:
                                trade["tracking_gap"] = True
                            trade["last_bar_end"] = bar.end
                            # OHLC cannot reveal the path when both levels touch.
                            # The adverse stop takes priority on that candle.
                            if stop is not None and bar.l <= stop:
                                close_trade(document, trade, min(stop, bar.o), execution_now, "stop",
                                            fx=execution_fx, price_ms=bar.end+1, price_source="completed_bar_trigger")
                                break
                            if target is not None and bar.h >= target:
                                close_trade(document, trade, target, execution_now, "target",
                                            fx=execution_fx, price_ms=bar.end+1, price_source="completed_bar_trigger")
                                break
                    if trade["status"] == "active" and execution_quote:
                        if stop is not None and execution_quote["bid"] <= stop:
                            close_trade(document, trade, execution_quote["bid"], execution_now, "stop", fx=execution_fx)
                        elif target is not None and execution_quote["bid"] >= target:
                            close_trade(document, trade, execution_quote["bid"], execution_now, "target", fx=execution_fx)
            current = (not problem and signal is not None and "clock" not in errors
                       and signal.get("signal_end") == (execution_now//H4)*H4-1
                       and reading.get("expires_ms", 0) > execution_now)
            if not current:
                record["last_result"] = problem or "Waiting for the latest completed Parente 4H signal"
                return
            end = signal["signal_end"]
            if self._baseline_conflict(record.get("last_signal"), signal):
                problem_text = "Rejected conflicting Parente signal for the same completed candle and input"
                record["last_result"] = problem_text
                row["neural"].update(signal=None, error=problem_text)
                return
            record["model_id"] = signal["model_id"]
            record["last_signal_end"] = max(record["last_signal_end"], end)
            record["last_signal_interval_ms"] = H4
            record["last_signal"] = copy.deepcopy(signal)
            record["last_signal_observed_ms"] = execution_now
            label = signal["label"]
            if label == "SELL":
                actionable = [t for t in record["trades"] if t["status"] == "active"
                              and t.get("model_id") == signal["model_id"]
                              and (end > t["exit_after_signal_end"] if t.get("entry_origin") == "manual"
                                   else end > t["signal_end"])]
                if actionable and execution_quote and execution_quote["asof_ms"] > end:
                    for trade in actionable:
                        close_trade(document, trade, execution_quote["bid"], execution_now, "Parente SELL",
                                    exit_signal=signal, fx=execution_fx)
                    record["last_result"] = "Exited on opposing Parente SELL"
                else:
                    record["last_result"] = "SELL: no eligible older paper entry or fresh executable quote"
                return
            if label != "BUY":
                record["last_result"] = "Parente HOLD; waiting for SMC entry and Parente BUY confirmation"
                return
            if not execution_quote or execution_quote["asof_ms"] <= end:
                record["last_result"] = "Waiting for a fresh executable quote after Parente confirmation"
                return
            if not valid_low or "stop_history" in errors:
                record["last_result"] = "Waiting for completed stop/target-monitoring history"
                return
            if (execution_quote["ask"]/execution_quote["bid"]-1)*10000 > self.rules.max_spread_bps:
                record["last_result"] = "Spread exceeds maximum"
                return
            try:
                plan = self._qualified_plan(smc_market, execution_quote, execution_now, self.rules.smc_min_exit_distance_pct, self.rules)
                trade = open_combined_trade(document, name, signal, plan, execution_quote,
                                            self.rules, execution_now, fx=execution_fx)
                record["last_result"] = "SMC entry opened with Parente BUY confirmation"
                row["neural"]["entry_plan"] = copy.deepcopy(trade["smc_entry_plan"])
            except (DataError, KeyError, TypeError) as exc:
                record["last_result"] = str(exc)

        self.store.transaction(update)
        record = self.store.snapshot()["assets"][name]
        active = copy.deepcopy([t for t in record["trades"] if t["status"] == "active"])
        row["neural"].update(result=record["last_result"], trades=active,
            trade=active[0] if active else None, active_count=len(active),
            status="ACTIVE PAPER TRADE" if len(active) == 1 else
                   f"{len(active)} ACTIVE PAPER TRADES" if active else "WAITING")
        return row


def current_signal(reading, now):
    reading = reading or {}
    signal = reading.get("signal")
    interval = H4
    expected_end = (now//interval)*interval-1
    if (reading.get("error") or not signal or signal.get("label") not in {"BUY", "HOLD", "SELL"}
            or signal.get("signal_end") != expected_end or now >= reading.get("expires_ms", 0)):
        return None
    return signal

