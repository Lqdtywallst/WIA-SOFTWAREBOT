from app.config import Settings
from app.models import TradeDecision, TradingViewAlert


def evaluate_signal(alert: TradingViewAlert, settings: Settings) -> TradeDecision:
    state = alert.state.upper()
    bias = alert.bias.upper()

    if settings.reject_on_rango_state and "RANGO" in state:
        return _reject(alert, "state contains RANGO")

    if settings.reject_on_no_chase_state and "NO CHASE" in state:
        return _reject(alert, "state contains NO CHASE")

    max_dist = settings.max_dist_vwap_atr
    if alert.dist_vwap_atr > max_dist:
        return _reject(alert, f"dist_vwap_atr is greater than {max_dist}")

    if alert.side == "short":
        if "BAJISTA" not in bias:
            return _reject(alert, "short signal requires BAJISTA bias")
        min_s = settings.min_short_score
        if alert.short_score < min_s:
            return _reject(alert, f"short_score is below {min_s}")
        return _allow(alert, "short setup accepted")

    if "ALCISTA" not in bias:
        return _reject(alert, "long signal requires ALCISTA bias")
    min_l = settings.min_long_score
    if alert.long_score < min_l:
        return _reject(alert, f"long_score is below {min_l}")
    return _allow(alert, "long setup accepted")


def _allow(alert: TradingViewAlert, reason: str) -> TradeDecision:
    return TradeDecision(allowed=True, reason=reason, alert=alert)


def _reject(alert: TradingViewAlert, reason: str) -> TradeDecision:
    return TradeDecision(allowed=False, reason=reason, alert=alert)
