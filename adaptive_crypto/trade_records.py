"""Read-only presentation of the saved neural paper ledger."""
import copy
from .position_options import paper_key


def paper_records(runtime):
    preferences = runtime.positions.snapshot().get("paper_alert_preferences", {})
    document = runtime.store.snapshot()
    trades = []
    for asset, record in document.get("assets", {}).items():
        for saved in record.get("trades", []):
            trade = copy.deepcopy(saved)
            trade.update(asset=asset, model="NN", model_active=True,
                         account_currency=document["account_currency"], quote_currency=saved["symbol"].split("/")[-1],
                         symbol=saved.get("symbol") or record.get("symbol")
                         or runtime.assets.get(asset, {}).get("symbol", asset+'/USDC'),
                         side=saved.get("side", "long"), paper_key=paper_key("neural", saved['id']))
            if trade["status"] == "active" and trade.get("target") is not None and hasattr(runtime,"market"):
                from .core import Rules
                from .take_profit_alerts import fresh_quote
                from .smc_reachability import assess_target
                import time
                now=int(time.time()*1000)
                market=runtime.market.get(asset,{})
                quote=fresh_quote(market.get("quote"),now) if market.get("symbol")==trade["symbol"] else None
                trade["target_reachability"]=assess_target(market.get("reachability_context"),
                    quote["bid" if trade["side"]=="long" else "ask"] if quote else None,
                    trade["target"],trade["side"],getattr(runtime,"rules",Rules()),now,trade["symbol"])
            trade.update(preferences.get(trade["paper_key"], {}))
            trades.append(trade)
    trades.sort(key=lambda t: t.get("closed_ms", t.get("opened_ms", 0)), reverse=True)
    return {"active": [t for t in trades if t["status"] == "active"],
            "closed": [t for t in trades if t["status"] != "active"], "errors": []}
