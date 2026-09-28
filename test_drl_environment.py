import importlib.util
import math
import unittest


@unittest.skipUnless(importlib.util.find_spec('gymnasium'), 'Isolated DRL training environment required')
class DRLEnvironmentTests(unittest.TestCase):
    def env(self, **kwargs):
        import numpy as np
        from tools.drl_environment import MarketSignalEnv
        return MarketSignalEnv(np.zeros((3, 19), dtype=np.float32), [100, 120, 80],
                               [110, 90, 100], episode_bars=3, random_start=False, **kwargs)

    def test_rewards_telescope_to_after_cost_final_equity(self):
        env = self.env()
        env.reset(seed=11)
        rewards = []
        for action in (0, 1, 2):
            _, r, done, truncated, info = env.step(action)
            rewards.append(r)
        expected = 80 * (1 - .0005) * (1 - .0005) * (1 - .004) / (100 * (1 + .0005) * (1 + .0005) * (1 + .004))
        self.assertAlmostEqual(info['equity'], expected)
        self.assertAlmostEqual(math.exp(sum(rewards) / 100), expected)
        self.assertEqual(info['natural_exits'], 1)
        self.assertTrue(done)
        self.assertFalse(truncated)
        with self.assertRaises(RuntimeError):
            env.step(1)

    def test_hold_cash_and_repeated_buy_terminal_costs(self):
        cash = self.env()
        cash.reset()
        for _ in range(3):
            _, reward, _, _, info = cash.step(1)
            self.assertEqual(reward, 0)
        self.assertEqual(info['equity'], 1)
        bought = self.env()
        bought.reset()
        for _ in range(3):
            _, _, _, _, info = bought.step(0)
        self.assertEqual(info['entries'], 1)
        self.assertEqual(info['natural_exits'], 0)
        self.assertEqual(info['terminal_exits'], 1)
        self.assertLess(info['equity'], 1)  # Flat start/end price still pays costs.

    def test_gym_contract_and_observation_never_contains_future_execution(self):
        from gymnasium.utils.env_checker import check_env
        import numpy as np
        a, b = self.env(), self.env()
        b.opens[:] *= 100
        b.closes[:] *= 100
        np.testing.assert_array_equal(a.reset(seed=23)[0], b.reset(seed=23)[0])
        check_env(a, skip_render_check=True)
