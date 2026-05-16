# TradingView — configuración optimizada (ZEC, crypto, bot)

## Script a copiar

Usa **`VWAP_Pro_INSTITUTIONAL_WEBHOOK_COMPLETO.pine`** (versión optimizada).

## Inputs recomendados para ZEC/USDT · Binance · 5m

| Grupo | Input | Valor |
|--------|--------|--------|
| Webhook | Preset mercado | **Crypto 24/7** |
| Webhook | Symbol = ticker del gráfico | **true** → envía `ZECUSDT` |
| Webhook | JSON alineado al bot | **true** → `state: TENDENCIA`, bias ALCISTA/BAJISTA |
| Webhook | WEBHOOK_SECRET | Igual que Railway (o vacío en ambos) |
| Control | Operar solo en sesión | **false** (crypto 24h) |
| Alertas | Activar alertas | **true** |

## Alerta en TradingView

1. Gráfico con el indicador guardado.
2. **Alerta** → condición: **Any alert() function call**.
3. **Webhook URL:** `https://wia-softwarebot-production.up.railway.app/webhook`
4. Frecuencia: **Once per bar close** (igual que el Pine).

## Qué mejoró la optimización

- **Un solo `request.security`** (HTF más rápido).
- **JSON compatible con el bot** (menos rechazos por `RANGO` / `NO CHASE` en el campo `state`).
- **Símbolo automático** del gráfico (`ZECUSDT`, etc.).
- **Preset crypto**: distancia VWAP/ATR mínima 5 en modo agresivo.
- **Menos `plotshape`** y límites de dibujo más bajos (mejor rendimiento).

## Railway (opcional, crypto)

```env
TRADING_MODE=paper
MAX_DIST_VWAP_ATR=6
MIN_LONG_SCORE=7
MIN_SHORT_SCORE=7
SYMBOL_MAP={"ZECUSDT":"TU_EPIC_IG_SI_APLICA"}
```

## Comprobar recepción

- https://wia-softwarebot-production.up.railway.app/history/alerts?limit=10
- https://wia-softwarebot-production.up.railway.app/dashboard
