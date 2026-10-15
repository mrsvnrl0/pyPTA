"""Offline numerical checks for the signed gamma-exposure build_profile formula."""
import math
import unittest

from adaptive_crypto.core import DataError
from adaptive_crypto.gex import build_profile, exposure, YEAR_MS

NOW = 1788793200000


def chain(specs=((110, "call", 30), (90, "put", 30)), base="BTC"):
    instruments, books = [], []
    for i, (strike, kind, oi) in enumerate(specs):
        name = f"{base}-{i}"
        instruments.append({"instrument_name": name, "base_currency": base, "kind": "option",
                            "is_active": True, "expiration_timestamp": NOW + YEAR_MS * .25,
                            "strike": strike, "option_type": kind, "contract_size": 100})
        books.append({"instrument_name": name, "creation_timestamp": NOW,
                      "open_interest": oi, "mark_iv": 50, "underlying_price": 100})
    return instruments, books, 100


def profile(specs=((110, "call", 30), (90, "put", 30))):
    instruments, books, spot = chain(specs)
    return build_profile(instruments, books, "BTC", spot, NOW)


class GammaMathTests(unittest.TestCase):
    def test_duplicate_feed_rows_are_rejected_instead_of_double_counting_exposure(self):
        instruments, books, spot = chain()
        with self.assertRaisesRegex(DataError, "duplicate active instrument"):
            build_profile(instruments + [instruments[0]], books, "BTC", spot, NOW)
        with self.assertRaisesRegex(DataError, "duplicate instrument summary"):
            build_profile(instruments, books + [books[0]], "BTC", spot, NOW)

    def test_gamma_units_and_sign_match_analytic_atm_value(self):
        option = {"strike": 100, "iv": .5, "years": .25, "oi": 30, "sign": 1}
        expected = math.exp(-.125 ** 2 / 2) / (math.sqrt(2 * math.pi) * 100 * .25) * 30 * 100 ** 2 * .01
        self.assertAlmostEqual(exposure(option, 100), expected)
        self.assertAlmostEqual(exposure({**option, "sign": -1}, 100), -expected)

    def test_walls_use_separate_call_put_exposure_and_never_double_multiply_oi(self):
        result = profile(((100, "call", 100), (100, "put", 99), (110, "call", 30), (90, "put", 30)))
        self.assertEqual(result["call_wall"]["strike"], 100)
        self.assertEqual(result["put_wall"]["strike"], 100)
        atm = next(r for r in result["strikes"] if r["strike"] == 100)
        self.assertAlmostEqual(atm["net_gex"],
                               exposure({"strike": 100, "iv": .5, "years": .25, "oi": 1, "sign": 1}, 100))

    def test_flip_matches_analytic_two_strike_solution(self):
        expected = math.sqrt(90 * 110) * math.exp(-.5 * .5 ** 2 * .25)
        result = profile()
        self.assertAlmostEqual(result["gamma_flip"], expected, places=7)
        self.assertEqual(len(result["flip_candidates"]), 1)

    def test_multiple_flips_nearest_to_index_is_selected(self):
        result = profile(((80, "call", 30), (100, "put", 50), (120, "call", 30)))
        self.assertEqual(len(result["flip_candidates"]), 2)
        self.assertEqual(result["gamma_flip"], min(result["flip_candidates"], key=lambda p: abs(p - 100)))

    def test_one_sided_and_balanced_chains_do_not_invent_walls_or_flips(self):
        for kind in ("call", "put"):
            result = profile(((100, kind, 30),))
            self.assertIsNone(result["gamma_flip"])
            self.assertIsNone(result["put_wall" if kind == "call" else "call_wall"])
        result = profile(((100, "call", 30), (100, "put", 30)))
        self.assertEqual(result["regime"], "neutral")
        self.assertIsNone(result["gamma_flip"])
        self.assertEqual(result["call_wall"]["strike"], 100)
        self.assertEqual(result["put_wall"]["strike"], 100)

    def test_other_assets_expired_and_zero_oi_are_excluded(self):
        instruments, books, spot = chain()
        instruments += [{**instruments[0], "base_currency": "ETH"},
                        {**instruments[0], "expiration_timestamp": NOW}]
        zero, summary, _ = chain(((150, "call", 0),))
        zero[0]["instrument_name"] = summary[0]["instrument_name"] = "zero"
        summary[0]["mark_iv"] = None
        result = build_profile(instruments + zero, books + summary, "BTC", spot, NOW)
        self.assertEqual(result["option_count"], 2)
        self.assertEqual(result["eligible_count"], 3)

    def test_incomplete_stale_future_and_invalid_positive_oi_fail_closed(self):
        for field, value in (("mark_iv", None), ("mark_iv", 0), ("mark_iv", float("nan")),
                             ("open_interest", -1), ("open_interest", True),
                             ("creation_timestamp", NOW - 300001), ("creation_timestamp", NOW + 2001)):
            with self.subTest(field=field, value=value):
                inst, books, spot = chain()
                books[0][field] = value
                with self.assertRaises(DataError):
                    build_profile(inst, books, "BTC", spot, NOW)
        inst, books, spot = chain()
        with self.assertRaises(DataError):
            build_profile(inst, books[:1], "BTC", spot, NOW)
        with self.assertRaises(DataError):
            profile(((100, "call", 0),))


if __name__ == "__main__":
    unittest.main()
