from app.models import TradeDecision, TradingViewAlert


def evaluate_signal(alert: TradingViewAlert) -> TradeDecision:
    state = alert.state.upper()
    bias = alert.bias.upper()

    if "RANGO" in state:
        return _reject(alert, "state contains RANGO")

    if "NO CHASE" in state:
        return _reject(alert, "state contains NO CHASE")

    if alert.dist_vwap_atr > 4.0:
        return _reject(alert, "dist_vwap_atr is greater than 4.0")

    if alert.side == "short":
        if "BAJISTA" not in bias:
            return _reject(alert, "short signal requires BAJISTA bias")
        if alert.short_score < 7:
            return _reject(alert, "short_score is below 7")
        return _allow(alert, "short setup accepted")

    if "ALCISTA" not in bias:
        return _reject(alert, "long signal requires ALCISTA bias")
    if alert.long_score < 7:
        return _reject(alert, "long_score is below 7")
    return _allow(alert, "long setup accepted")


def _allow(alert: TradingViewAlert, reason: str) -> TradeDecision:
    return TradeDecision(allowed=True, reason=reason, alert=alert)


def _reject(alert: TradingViewAlert, reason: str) -> TradeDecision:
    return TradeDecision(allowed=False, reason=reason, alert=alert)
