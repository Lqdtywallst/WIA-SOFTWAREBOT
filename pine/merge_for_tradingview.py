#!/usr/bin/env python3
"""
Genera el Pine listo para TradingView: inserta webhook JSON en tu indicador original.

Uso:
  python3 merge_for_tradingview.py RUTA_A_TU_INDICADOR.pine > SALIDA_WEBHOOK.pine

Opciones:
  --no-score-bump   No cambia min_signal_score de 6 a 7 (el bot exige score>=7).
"""
from __future__ import annotations

import argparse
import pathlib
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path, help="Archivo .pine con tu indicador actual (copiado desde Pine Editor)")
    parser.add_argument("--no-score-bump", action="store_true", help="No subir min_signal_score por defecto a 7")
    args = parser.parse_args()

    base = pathlib.Path(__file__).resolve().parent
    text = args.source.read_text(encoding="utf-8")
    block_a = (base / "BLOQUE_A_webhook_inputs.txt").read_text(encoding="utf-8").strip("\n") + "\n\n"
    block_b = (base / "BLOQUE_B_webhook_functions.txt").read_text(encoding="utf-8").strip("\n") + "\n\n"
    block_c = (base / "BLOQUE_C_alertas_json.txt").read_text(encoding="utf-8").strip("\n") + "\n"

    needle = 'alert_symbol_name = input.string("YMM / US30 / DOW", "Nombre Mercado", group=grp7)\n'
    if needle not in text:
        sys.exit(
            "ERROR: No encontré la línea exacta de alert_symbol_name.\n"
            "Asegúrate de que tu script es el mismo indicador (nombre mercado por defecto igual).\n"
            f"Buscaba:\n{needle!r}"
        )
    text = text.replace(needle, needle + "\n" + block_a, 1)

    marker = "// ═══════════════════════════════════════════\n// ALERTAS PROFESIONALES"
    if marker not in text:
        sys.exit("ERROR: No encontré el bloque '// ALERTAS PROFESIONALES'. ¿Es exactamente el mismo script?")
    text = text.replace(marker, block_b + marker, 1)

    old_alerts = '''if use_alerts and long_sig
    alert("VWAP PRO LONG | " + alert_symbol_name +
          " | Precio: " + str.tostring(close, "#.##") +
          " | Bias: " + bias_txt +
          " | Estado: " + institutional_state +
          " | LONG Score: " + str.tostring(long_score) + "/10" +
          " | ADX: " + str.tostring(adx, "#.##") +
          " | RSI: " + str.tostring(rsi, "#.##") +
          " | Vol: " + str.tostring(vol_ratio, "#.##") + "x" +
          " | DistVWAP: " + str.tostring(dist_vwap_atr, "#.##") + " ATR",
          alert.freq_once_per_bar_close)

if use_alerts and short_sig
    alert("VWAP PRO SHORT | " + alert_symbol_name +
          " | Precio: " + str.tostring(close, "#.##") +
          " | Bias: " + bias_txt +
          " | Estado: " + institutional_state +
          " | SHORT Score: " + str.tostring(short_score) + "/10" +
          " | ADX: " + str.tostring(adx, "#.##") +
          " | RSI: " + str.tostring(rsi, "#.##") +
          " | Vol: " + str.tostring(vol_ratio, "#.##") + "x" +
          " | DistVWAP: " + str.tostring(dist_vwap_atr, "#.##") + " ATR",
          alert.freq_once_per_bar_close)'''

    if old_alerts not in text:
        sys.exit(
            "ERROR: No encontré los dos bloques alert() originales (LONG y SHORT).\n"
            "¿Has cambiado el texto de las alertas? Si es así, dímelo y adaptamos el script."
        )
    text = text.replace(old_alerts, block_c, 1)

    if not args.no_score_bump:
        old_score = 'min_signal_score      = input.int(6, "Score mínimo para señal"'
        new_score = 'min_signal_score      = input.int(7, "Score mínimo para señal"'
        if old_score in text:
            text = text.replace(old_score, new_score, 1)

    sys.stdout.write(text)


if __name__ == "__main__":
    main()
