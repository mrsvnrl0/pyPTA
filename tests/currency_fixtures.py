"""Explicit captured FX quotes for deterministic paper-account tests."""
from adaptive_crypto.currency_conversion import identity_fx


def fx(now, currency="USDC", buy=1., sell=None):
    if currency=="USD": return identity_fx(currency,now)
    sell=buy if sell is None else sell
    source={"from_currency":currency,"to_currency":"USD","pair":currency+"/USD",
            "direction":"direct","venue":"Deribit","bid":sell,"ask":buy,
            "buy_rate":buy,"sell_rate":sell,"asof_ms":now,"expires_ms":now+20000}
    return {"quote_currency":currency,"account_currency":"USD","buy_rate":buy,
            "sell_rate":sell,"asof_ms":now,"expires_ms":now+20000,"sources":[source]}
