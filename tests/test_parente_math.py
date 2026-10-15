"""Parente feature causality, archived author outputs and read-only simulation."""
import copy,json,tempfile,unittest
from pathlib import Path
from adaptive_crypto.core import Candle,DataError,H4
from adaptive_crypto.neural import CLASSES,FEATURES,NeuralModel,feature_frame
from adaptive_crypto.neural_tools import simulate
from tests.neural_fixtures import bars

class NeuralMathTests(unittest.TestCase):

    def test_features_and_inference_match_archived_author_reference(self):
        import numpy as np
        fixture = json.loads((Path(__file__).parent / 'data/neural_author_btc.json').read_text())
        model = NeuralModel()
        candles = [Candle(**row) for row in fixture['candles']]
        values = model.features(candles, 'BTC').iloc[-3:].to_numpy()
        np.testing.assert_allclose(values, fixture['features'], atol=1e-10, rtol=1e-09)
        np.testing.assert_allclose(model.probabilities(values), fixture['probabilities'], atol=2e-06, rtol=2e-05)

    def test_feature_prefix_is_unchanged_by_future_candles(self):
        import numpy as np
        candles = bars(count=180)
        prefix = feature_frame(candles[:150], (1200, 200))
        full = feature_frame(candles, (1200, 200))
        self.assertEqual(list(full), list(FEATURES))
        self.assertEqual(full.shape, (180, 36))
        np.testing.assert_allclose(prefix, full.iloc[:150], equal_nan=True)
        self.assertTrue(np.isfinite(full.iloc[-1]).all())

    def test_model_description_identifies_the_current_input_contract(self):
        model = NeuralModel()
        self.assertIn("Binance spot/USDT completed 4H candles", model.metadata["description"])
        self.assertIn("frozen Binance base-volume calibration", model.metadata["description"])

    def test_model_outputs_finite_probabilities_and_rejects_bad_features(self):
        import numpy as np
        model = NeuralModel()
        result = model.predict(bars(), 'BTC')
        self.assertAlmostEqual(sum(result['probabilities'].values()), 1.0, places=6)
        self.assertIn(result['label'], CLASSES)
        for bad in ([1, 2], np.full((1, 36), np.nan)):
            with self.assertRaises(DataError):
                model.probabilities(bad)
        with self.assertRaisesRegex(DataError, 'calibration'):
            model.predict(bars(), 'UNKNOWN')

    def test_bad_model_metadata_and_weights_fail_closed(self):
        import numpy as np
        model = NeuralModel()
        with np.load(model.path, allow_pickle=False) as data:
            original = {k: data[k].copy() for k in data.files}
        with tempfile.TemporaryDirectory() as tmp:
            for case in ('classes', 'volume', 'weights', 'scale'):
                arrays = copy.deepcopy(original)
                metadata = json.loads(str(arrays['metadata'].item()))
                if case == 'classes':
                    metadata['classes'].reverse()
                elif case == 'volume':
                    metadata['volume_stats']['BTC'] = [100, 0]
                elif case == 'weights':
                    arrays['w0'][0, 0] = np.nan
                else:
                    arrays['scale'][0] = 0
                arrays['metadata'] = np.array(json.dumps(metadata))
                path = Path(tmp) / f'{case}.npz'
                np.savez(path, **arrays)
                with self.subTest(case=case), self.assertRaises(DataError):
                    NeuralModel(path)

    def test_backtest_uses_next_open_and_charges_both_sides(self):
        candles = [Candle(i * H4, p, p + 1, p - 1, p, 1, H4) for i, p in enumerate((100, 110, 120, 130))]
        result = simulate(candles, ['BUY', 'HOLD', 'SELL', 'BUY'], fee=0.001, slippage=0.0005)
        trade, = result['trades']
        self.assertAlmostEqual(trade['entry'], 110 * 1.0005)
        self.assertAlmostEqual(trade['exit'], 130 * 0.9995)
        self.assertAlmostEqual(result['final'], 1000 / (110 * 1.0005 * 1.001) * 130 * 0.9995 * 0.999)
        self.assertEqual(trade['opened_ms'], H4)
        self.assertEqual(trade['closed_ms'], 3 * H4)

    def test_backtest_gap_stop_precedes_sell_and_no_same_bar_reentry(self):
        candles = [Candle(i * H4, p, p + 1, p - 1, p, 1, H4) for i, p in enumerate((100, 100, 80))]
        for signal in ('SELL', 'BUY'):
            result = simulate(candles, ['BUY', signal, 'HOLD'], fee=0, slippage=0)
            self.assertEqual(result['trade_count'], 1)
            self.assertEqual(result['trades'][0]['reason'], 'gap_stop')
            self.assertEqual(result['final'], 800)
        with self.assertRaises(DataError):
            simulate([], [])
