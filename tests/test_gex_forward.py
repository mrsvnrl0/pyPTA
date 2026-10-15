"""Independent derivative checks for expiry-forward GEX estimates."""
import math
import unittest

from adaptive_crypto.core import DataError
from adaptive_crypto.gex import YEAR_MS, build_profile, exposure
from tests.test_gex_formula import NOW, chain


class ForwardGammaTests(unittest.TestCase):
    def test_exposure_matches_finite_difference_of_delta_with_fixed_carry(self):
        # Differentiate N(d1) numerically; do not repeat the gamma expression.
        for ratio in (.92, 1., 1.15):
            for spot in (70., 100., 135.):
                option = dict(strike=105., iv=.4, years=.7, oi=37., sign=1,
                              forward_ratio=ratio)
                width = option['iv'] * math.sqrt(option['years'])
                def delta(price):
                    d1 = (math.log(price * ratio / option['strike']) + width**2/2)/width
                    return .5 * (1 + math.erf(d1/math.sqrt(2)))
                step = spot * 1e-5
                expected = (delta(spot+step)-delta(spot-step))/(2*step)*37*spot**2*.01
                with self.subTest(ratio=ratio, spot=spot):
                    self.assertAlmostEqual(exposure(option, spot), expected, places=6)
                    self.assertAlmostEqual(exposure({**option, 'sign': -1}, spot), -expected, places=6)

    def test_forward_shifts_analytic_flip_without_double_counting_carry(self):
        instruments, books, spot = chain()
        for book in books:
            book['underlying_price'] = 105.
            # This field is not the index/forward basis; do not apply it again.
            book['interest_rate'] = .12
        profile = build_profile(instruments, books, 'BTC', spot, NOW)
        expected = math.sqrt(90*110)*math.exp(-.5*.5**2*.25)/1.05
        self.assertAlmostEqual(profile['gamma_flip'], expected, places=7)
        self.assertEqual(profile['forward_priced_count'], 2)
        self.assertEqual(profile['pricing_model'], 'forward-basis-spot-delta-v2')

    def test_missing_forward_rejected_but_zero_oi_needs_no_forward(self):
        instruments, books, spot = chain(((110, 'call', 30), (90, 'put', 30), (120, 'call', 0)))
        books[2].pop('underlying_price')
        result = build_profile(instruments, books, 'BTC', spot, NOW)
        self.assertEqual(result['eligible_count'], 3)
        self.assertEqual(result['forward_priced_count'], 2)
        books[0].pop('underlying_price')
        with self.assertRaises(DataError):
            build_profile(instruments, books, 'BTC', spot, NOW)

    def test_invalid_supplied_forward_is_not_silently_replaced(self):
        for value in (None, 0, -1, True, float('nan'), float('inf')):
            instruments, books, spot = chain()
            books[0]['underlying_price'] = value
            with self.subTest(value=value), self.assertRaises(DataError):
                build_profile(instruments, books, 'BTC', spot, NOW)

    def test_gross_exposure_preserves_cancellation_and_oi_units(self):
        instruments, books, spot = chain(((100, 'call', 30), (100, 'put', 30)), base='SOL')
        for instrument in instruments:
            instrument['contract_size'] = 10
        for book in books:
            book['underlying_price'] = 105
        profile = build_profile(instruments, books, 'SOL', spot, NOW)
        self.assertEqual(profile['net_gex'], 0)
        self.assertGreater(profile['gross_gex'], 0)
        self.assertEqual(profile['net_to_gross'], 0)
        for instrument in instruments:
            instrument['contract_size'] = 1
        self.assertEqual(profile['gross_gex'], build_profile(instruments, books, 'SOL', spot, NOW)['gross_gex'])

    def test_expiry_uses_act_365(self):
        self.assertEqual(YEAR_MS, 365*86400000)
        instruments, books, spot = chain(((100, 'call', 1),))
        instruments[0]['expiration_timestamp'] = NOW + 365*86400000
        books[0]['underlying_price'] = spot
        result = build_profile(instruments, books, 'BTC', spot, NOW)
        self.assertEqual(result['day_count'], 'ACT/365')
        self.assertAlmostEqual(result['net_gex'], exposure(dict(strike=100, iv=.5, years=1, oi=1, sign=1), spot))


if __name__ == '__main__':
    unittest.main()
