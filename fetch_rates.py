"""
fetch_rates.py
--------------
Script de una sola ejecución (no es un servidor HTTP).
Obtiene tasas BCV y Binance P2P, actualiza el historial CSV
y escribe data/output.json listo para que TRMNL lo consuma.

Uso:
    python fetch_rates.py

GitHub Actions lo ejecuta cada 15 minutos automáticamente.
"""

import datetime as dt
import json
import logging
import os
import sys

# Importamos toda la lógica desde rates_server.py
# (ambos archivos deben estar en la misma carpeta)
from rates_server import (
    ensure_storage,
    build_full_payload,
    build_summary_payload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_JSON = os.path.join(DATA_DIR, "output.json")


def main():
    logging.info("=== fetch_rates.py iniciando ===")
    ensure_storage()

    try:
        full_payload = build_full_payload()
        summary_payload = build_summary_payload(full_payload)

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(summary_payload, f, ensure_ascii=False, indent=2)

        logging.info(f"BCV USD/VES : {summary_payload['bcv']['usd_ves']}")
        logging.info(f"BCV USD/VES guardado: {summary_payload['bcv']['usd_ves']}")
        logging.info(f"Binance BUY : {summary_payload['binance_prices']['buy_now']['median']}")
        logging.info(f"Binance SELL: {summary_payload['binance_prices']['sell_now']['median']}")
        logging.info(f"Brecha ahora: {summary_payload['brecha']['now_pct']:.2f}%")
        logging.info(f"JSON escrito en: {OUTPUT_JSON}")
        logging.info("=== fetch_rates.py completado ===")

    except Exception as e:
        logging.error(f"Error al obtener tasas: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
