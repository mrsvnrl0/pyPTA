"""Offline long-only environment for a deliberately reactive market-signal policy.

The actor does not observe holdings: the same completed candle must yield the
same signal for every dashboard consumer. This is a partially observed policy,
not a fully observed portfolio optimizer. Fit each asset independently; assess
the resulting signals again with the existing shared-cash portfolio replay.
"""
from __future__ import annotations

import math
import numpy as np
import gymnasium as gym
from gymnasium import spaces


class MarketSignalEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, observations, execution_open, execution_close, *,
                 fee=.004, slippage=.0005, spread_bps=10., episode_bars=128,
                 random_start=True):
        super().__init__()
        self.observations = np.asarray(observations, dtype=np.float32)
        self.opens = np.asarray(execution_open, dtype=np.float64)
        self.closes = np.asarray(execution_close, dtype=np.float64)
        n = len(self.observations)
        if (self.observations.shape != (n, 19) or n < 2 or self.opens.shape != (n,)
                or self.closes.shape != (n,) or not np.isfinite(self.observations).all()
                or not np.isfinite(self.opens).all() or not np.isfinite(self.closes).all()
                or min(self.opens.min(), self.closes.min()) <= 0):
            raise ValueError("Invalid aligned DRL observations/execution bars")
        if not 0 <= fee < .05 or not 0 <= slippage < .05 or not 0 <= spread_bps < 1000:
            raise ValueError("Invalid DRL costs")
        if type(episode_bars) is not int or episode_bars < 2:
            raise ValueError("Invalid episode length")
        self.fee, self.slippage, self.half_spread = fee, slippage, spread_bps / 20000
        self.length = min(episode_bars, n)
        self.random_start = random_start
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(19,), dtype=np.float32)
        self.action_space = spaces.Discrete(3)  # BUY, HOLD, SELL
        self.done = True

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.index = int(self.np_random.integers(0, len(self.opens) - self.length + 1)) if self.random_start else 0
        self.end = self.index + self.length
        self.cash, self.quantity, self.equity = 1., 0., 1.
        self.natural_exits = self.terminal_exits = self.entries = 0
        self.done = False
        return self.observations[self.index].copy(), {}

    def _sale_value(self, midpoint):
        return self.quantity * midpoint * (1 - self.half_spread) * (1 - self.slippage) * (1 - self.fee)

    def step(self, action):
        if self.done:
            raise RuntimeError("Reset the completed DRL episode before stepping")
        if not self.action_space.contains(action):
            raise ValueError("DRL action must be BUY/HOLD/SELL index")
        before = self.equity
        if action == 0 and self.cash > 1e-12:
            ask_fill = self.opens[self.index] * (1 + self.half_spread) * (1 + self.slippage)
            self.quantity += self.cash / (ask_fill * (1 + self.fee))
            self.cash = 0.
            self.entries += 1
        elif action == 2 and self.quantity > 0:
            self.cash += self._sale_value(self.opens[self.index])
            self.quantity = 0.
            self.natural_exits += 1
        # Price of execution is the NEXT bar's open; the actor never sees this
        # bar until the following observation. Reward includes all exit costs.
        self.equity = self.cash + self._sale_value(self.closes[self.index])
        reward = 100 * math.log(self.equity / before)
        self.index += 1
        self.done = self.index == self.end
        if self.done and self.quantity > 0:
            self.cash = self.equity
            self.quantity = 0.
            self.terminal_exits += 1  # Already charged in net-liquidation mark.
        observation = self.observations[min(self.index, self.end - 1)].copy()
        info = {"equity": self.equity, "natural_exits": self.natural_exits,
                "terminal_exits": self.terminal_exits, "entries": self.entries}
        # A sampled finite trading episode liquidates by contract, so it is
        # terminal, not a time-limit truncation with a bootstrapped open position.
        return observation, reward, self.done, False, info
