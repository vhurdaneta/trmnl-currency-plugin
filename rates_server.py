from http.server import BaseHTTPRequestHandler, HTTPServer
import datetime as dt
import json
import logging
import os
import csv
from statistics import median, mean

import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)

BCV_URL = "https://www.bcv.org.ve"
BINANCE_P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

# Cada cuánto guardamos una muestra Binance (para no llenar el disco)
SAMPLE_INTERVAL_SECONDS = 300  # 5 minutos

# Hora a partir de la cual el BCV publica la tasa del día SIGUIENTE (hora Venezuela = UTC-4)
# Ejemplo: a las 17:30 VET publican la tasa del próximo día hábil
BCV_CUTOFF_HOUR = 17  # 5:00 PM hora Venezuela

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

HISTORY_CSV = os.path.join(DATA_DIR, "rates_history.csv")   # 1 fila por día
SAMPLES_CSV = os.path.join(DATA_DIR, "binance_samples.csv") # muestras intradía

CACHE = {"data": None, "updated_at": None, "error": None}


# ---------------------------------------------------------------------------
# Helpers de formato
# ---------------------------------------------------------------------------

def _to_float_ves(s: str) -> float:
    s = s.strip().replace(".", "").replace(",", ".")
    return float(s)


def normalize_date_str(s: str) -> str:
    """Convierte '10/3/2026' (día/mes/año) a '2026-03-10'."""
    s = (s or "").strip()
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            d = int(parts[0])
            m = int(parts[1])
            y = int(parts[2])
            return f"{y:04d}-{m:02d}-{d:02d}"
    return s


def dedupe_and_sort_history(rows: list) -> list:
    """Elimina duplicados por date quedándose con el último, y ordena."""
    dedup = {}
    for r in rows:
        key = r.get("date")
        if key:
            dedup[key] = r
    out = list(dedup.values())
    out.sort(key=lambda x: x.get("date", ""))
    return out


# ---------------------------------------------------------------------------
# Lógica clave: ¿a qué fecha corresponde la tasa del BCV ahora?
# ---------------------------------------------------------------------------

def bcv_effective_date(now: dt.datetime) -> str:
    """
    El BCV publica la tasa del DÍA HÁBIL SIGUIENTE a partir de las 5-6 PM
    del día hábil anterior.

    Regla implementada:
    - Si son las BCV_CUTOFF_HOUR o más (hora local Venezuela), la tasa
      publicada corresponde al PRÓXIMO día hábil → avanzamos la fecha.
    - Si es antes del corte, la tasa corresponde al día actual o al
      último día hábil pasado.
    - Siempre saltamos fines de semana: sábado → lunes, domingo → lunes.

    Retorna: string ISO 'YYYY-MM-DD' con la fecha efectiva de la tasa.
    """
    date = now.date()

    if now.hour >= BCV_CUTOFF_HOUR:
        # La tasa ya es del próximo día hábil
        date += dt.timedelta(days=1)
        # Saltar fines de semana
        while date.weekday() >= 5:  # 5=sábado, 6=domingo
            date += dt.timedelta(days=1)
    else:
        # Tasa del día actual; si es fin de semana, corresponde al lunes
        while date.weekday() >= 5:
            date += dt.timedelta(days=1)

    return date.isoformat()


def bcv_date_label(effective_date_str: str, now: dt.datetime) -> str:
    """
    Devuelve una etiqueta legible que indica si la tasa es de hoy,
    mañana, o el lunes (cuando es fin de semana).
    Útil para mostrar en la UI del plugin.
    """
    today = now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()

    if effective_date_str == today:
        return "hoy"
    elif effective_date_str == tomorrow:
        return "mañana"
    else:
        return f"próx. hábil ({effective_date_str})"


# ---------------------------------------------------------------------------
# Almacenamiento
# ---------------------------------------------------------------------------

def ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "date",           # fecha efectiva de la tasa BCV (YYYY-MM-DD)
                "bcv_usd_ves",
                "bcv_eur_ves",
                "bcv_locked",     # '1' si ya se fijó la tasa del día (no se sobreescribe)
                "usdt_buy_avg",
                "usdt_sell_avg",
                "samples_count",
                "first_sample_at",
                "last_sample_at",
            ])

    if not os.path.exists(SAMPLES_CSV):
        with open(SAMPLES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "date", "usdt_buy_median", "usdt_sell_median"])


# ---------------------------------------------------------------------------
# Fuentes de datos externas
# ---------------------------------------------------------------------------

def fetch_bcv_rate(currency_id: str, timeout=15) -> dict:
    r = requests.get(BCV_URL, timeout=timeout, verify=False,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    block = soup.find("div", {"id": currency_id})
    if not block:
        raise RuntimeError(f"No encontré el div id='{currency_id}' en BCV.")

    val_div = block.find("div", {"class": "col-sm-6 col-xs-6 centrado"})
    raw = val_div.get_text(strip=True) if val_div else block.get_text(" ", strip=True)
    rate = _to_float_ves(raw)

    return {"ves": rate, "source": BCV_URL}


def fetch_binance_ads_first_100(
    fiat="VES",
    asset="USDT",
    trade_type="BUY",
    limit=100,
    rows=20,
    timeout=15,
):
    if rows > 20:
        raise ValueError("rows debe ser <= 20 (limitación del endpoint).")

    ads = []
    page = 1

    while len(ads) < limit:
        body = {
            "fiat": fiat,
            "page": page,
            "rows": rows,
            "tradeType": trade_type,
            "asset": asset,
        }

        r = requests.post(
            BINANCE_P2P_URL,
            json=body,
            timeout=timeout,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0",
            },
        )
        r.raise_for_status()
        j = r.json()

        if j.get("code") != "000000":
            raise RuntimeError(
                f"Binance P2P respondió code={j.get('code')} message={j.get('message')}"
            )

        data = j.get("data", [])
        if not isinstance(data, list) or len(data) == 0:
            break

        for item in data:
            adv = item.get("adv", {}) or {}
            ads.append({
                "price": float(adv.get("price", 0) or 0),
                "min": float(adv.get("minSingleTransAmount", 0) or 0),
                "max": float(adv.get("maxSingleTransAmount", 0) or 0),
            })
            if len(ads) >= limit:
                break

        page += 1
        if page > 10:
            break

    prices = [a["price"] for a in ads if a["price"] > 0]
    stats = {
        "count": len(prices),
        "median": median(prices) if prices else None,
        "average": mean(prices) if prices else None,
        "min": min(prices) if prices else None,
        "max": max(prices) if prices else None,
    }

    return {
        "trade_type": trade_type,
        "prices": stats,
        "ads": ads,
        "source": BINANCE_P2P_URL,
    }


# ---------------------------------------------------------------------------
# Muestras intradía Binance
# ---------------------------------------------------------------------------

def read_last_sample_ts():
    if not os.path.exists(SAMPLES_CSV):
        return None
    last = None
    with open(SAMPLES_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            last = row
    if not last:
        return None
    try:
        return dt.datetime.fromisoformat(last["ts"])
    except Exception:
        return None


def record_sample_if_due(now: dt.datetime, buy_median: float, sell_median: float):
    if buy_median is None or sell_median is None:
        return

    last_ts = read_last_sample_ts()
    if last_ts is not None:
        if (now - last_ts).total_seconds() < SAMPLE_INTERVAL_SECONDS:
            return

    with open(SAMPLES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            now.isoformat(timespec="seconds"),
            now.date().isoformat(),
            buy_median,
            sell_median,
        ])


def aggregate_day(date_str: str):
    """Devuelve promedios del día usando las muestras intradía."""
    samples_buy = []
    samples_sell = []
    first_ts = None
    last_ts = None

    if not os.path.exists(SAMPLES_CSV):
        return None

    with open(SAMPLES_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["date"] != date_str:
                continue
            try:
                ts = dt.datetime.fromisoformat(row["ts"])
                b = float(row["usdt_buy_median"])
                s = float(row["usdt_sell_median"])
            except Exception:
                continue

            samples_buy.append(b)
            samples_sell.append(s)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    if not samples_buy or not samples_sell:
        return None

    return {
        "count": len(samples_buy),
        "buy_avg": mean(samples_buy),
        "sell_avg": mean(samples_sell),
        "buy_min": min(samples_buy),
        "buy_max": max(samples_buy),
        "sell_min": min(samples_sell),
        "sell_max": max(samples_sell),
        "first_sample_at": first_ts.isoformat(timespec="seconds"),
        "last_sample_at": last_ts.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Historial diario — lógica clave: tasa BCV bloqueada en primera escritura
# ---------------------------------------------------------------------------

def upsert_daily_row(
    effective_date: str,
    bcv_usd: float,
    bcv_eur: float,
    day_agg: dict,
):
    """
    Mantiene 1 fila por fecha efectiva BCV en rates_history.csv.

    Regla para bcv_usd / bcv_eur:
    - Si la fila NO existe → se crea con bcv_locked=1 (primera tasa del día,
      que es la correcta según la lógica del BCV venezolano).
    - Si la fila YA existe con bcv_locked=1 → NO se sobreescribe la tasa BCV.
      Solo se actualizan los promedios Binance del día.

    Esto garantiza que la tasa BCV que queda en el histórico es siempre
    la primera que se vio para esa fecha efectiva, evitando que la
    publicación de la tarde (que corresponde al próximo día hábil)
    contamine el registro.
    """
    ensure_storage()

    rows = []
    found = False

    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        fieldnames = r.fieldnames or []
        for row in r:
            if row["date"] == effective_date:
                found = True
                already_locked = row.get("bcv_locked", "0") == "1"

                if not already_locked:
                    # Primera vez que procesamos este día → grabamos tasa y bloqueamos
                    row["bcv_usd_ves"] = str(bcv_usd)
                    row["bcv_eur_ves"] = str(bcv_eur)
                    row["bcv_locked"] = "1"
                    logging.info(
                        f"[BCV] Tasa bloqueada para {effective_date}: "
                        f"USD={bcv_usd} EUR={bcv_eur}"
                    )
                else:
                    # Tasa ya bloqueada → solo loguear, no tocar
                    logging.info(
                        f"[BCV] Tasa ya bloqueada para {effective_date}, "
                        f"se ignora nuevo valor USD={bcv_usd}"
                    )

                # Siempre actualizamos los promedios Binance
                row["usdt_buy_avg"] = str(day_agg["buy_avg"])
                row["usdt_sell_avg"] = str(day_agg["sell_avg"])
                row["samples_count"] = str(day_agg["count"])
                row["first_sample_at"] = day_agg["first_sample_at"]
                row["last_sample_at"] = day_agg["last_sample_at"]

            rows.append(row)

    if not found:
        # Fila nueva: guardamos tasa y la bloqueamos de inmediato
        logging.info(
            f"[BCV] Nueva fila para {effective_date}: "
            f"USD={bcv_usd} EUR={bcv_eur} (bloqueada)"
        )
        rows.append({
            "date": effective_date,
            "bcv_usd_ves": str(bcv_usd),
            "bcv_eur_ves": str(bcv_eur),
            "bcv_locked": "1",
            "usdt_buy_avg": str(day_agg["buy_avg"]),
            "usdt_sell_avg": str(day_agg["sell_avg"]),
            "samples_count": str(day_agg["count"]),
            "first_sample_at": day_agg["first_sample_at"],
            "last_sample_at": day_agg["last_sample_at"],
        })

    # Aseguramos que fieldnames incluya bcv_locked aunque el CSV sea viejo
    all_keys = list(rows[0].keys())
    if "bcv_locked" not in all_keys:
        all_keys.insert(3, "bcv_locked")

    rows = dedupe_and_sort_history(rows)

    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_history_last_n(n=30):
    if not os.path.exists(HISTORY_CSV):
        return []

    out = []
    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            row["date"] = normalize_date_str(row.get("date", ""))
            out.append(row)

    out = dedupe_and_sort_history(out)
    return out[-n:]


# ---------------------------------------------------------------------------
# Utilidades de cálculo
# ---------------------------------------------------------------------------

def pct_change(today: float, yesterday: float):
    if today is None or yesterday is None or yesterday == 0:
        return None
    return ((today - yesterday) / yesterday) * 100.0


def normalize_series(values, vmin, vmax):
    if vmin is None or vmax is None or vmax == vmin:
        return [50 for _ in values]
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            out.append(int(round(((v - vmin) / (vmax - vmin)) * 100)))
    return out


# ---------------------------------------------------------------------------
# Construcción de payloads
# ---------------------------------------------------------------------------

def build_full_payload() -> dict:
    now = dt.datetime.now().astimezone()

    usd = fetch_bcv_rate("dolar")
    eur = fetch_bcv_rate("euro")

    buy = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="BUY")
    sell = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="SELL")

    effective_date = bcv_effective_date(now)
    date_label = bcv_date_label(effective_date, now)

    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "bcv": {
            "usd_ves": usd["ves"],
            "eur_ves": eur["ves"],
            "source": BCV_URL,
            "effective_date": effective_date,   # fecha a la que corresponde la tasa
            "date_label": date_label,           # 'hoy', 'mañana', 'próx. hábil (...)'
        },
        "binance": buy,
        "binance_sell": sell,
        "warning": None,
        "error": None,
    }


def build_summary_payload(full_payload: dict, history_days=30) -> dict:
    now = dt.datetime.now().astimezone()
    today_str = now.date().isoformat()

    bcv_usd = full_payload["bcv"]["usd_ves"]
    bcv_eur = full_payload["bcv"]["eur_ves"]
    effective_date = full_payload["bcv"]["effective_date"]

    buy_med = full_payload["binance"]["prices"]["median"]
    sell_med = full_payload["binance_sell"]["prices"]["median"]

    # 1) Guardar muestra intradía Binance (throttle por SAMPLE_INTERVAL_SECONDS)
    #    Las muestras Binance siguen usando la fecha real, no la efectiva BCV
    record_sample_if_due(now, buy_med, sell_med)

    # 2) Calcular agregado Binance del día real
    day_agg = aggregate_day(today_str)

    # 3) Actualizar fila del histórico usando la fecha EFECTIVA del BCV
    #    La tasa BCV se bloquea en la primera escritura para esa fecha
    if day_agg:
        upsert_daily_row(effective_date, bcv_usd, bcv_eur, day_agg)

    # 4) Leer histórico diario (últimos N días)
    hist = read_history_last_n(history_days)

    # Cambios vs el día anterior en el histórico
    change = {
        "bcv_usd": {"pct": None},
        "usdt_buy_avg": {"pct": None},
    }
    if len(hist) >= 2:
        y = hist[-2]
        t = hist[-1]
        try:
            change["bcv_usd"]["pct"] = pct_change(
                float(t["bcv_usd_ves"]), float(y["bcv_usd_ves"])
            )
        except Exception:
            pass
        try:
            change["usdt_buy_avg"]["pct"] = pct_change(
                float(t["usdt_buy_avg"]), float(y["usdt_buy_avg"])
            )
        except Exception:
            pass

    # Series para gráficas
    dates = [r["date"] for r in hist]
    bcv_series = []
    usdt_series = []

    for r in hist:
        try:
            bcv_series.append(float(r["bcv_usd_ves"]))
        except Exception:
            bcv_series.append(None)
        try:
            usdt_series.append(float(r["usdt_buy_avg"]))
        except Exception:
            usdt_series.append(None)

    all_vals = [v for v in bcv_series + usdt_series if v is not None]
    vmin = min(all_vals) if all_vals else None
    vmax = max(all_vals) if all_vals else None

    # Brecha (%) por día: (USDT - BCV) / BCV * 100
    brecha_series = []
    for i in range(len(bcv_series)):
        b = bcv_series[i]
        u = usdt_series[i]
        if b is None or u is None or b == 0:
            brecha_series.append(None)
        else:
            brecha_series.append(((u - b) / b) * 100.0)

    brecha_vals = [v for v in brecha_series if v is not None]
    bmin = min(brecha_vals) if brecha_vals else None
    bmax = max(brecha_vals) if brecha_vals else None

    chart = {
        "dates": dates,
        "bcv_norm": normalize_series(bcv_series, vmin, vmax),
        "usdt_norm": normalize_series(usdt_series, vmin, vmax),
        "vmin": vmin,
        "vmax": vmax,
        "brecha_norm": normalize_series(brecha_series, bmin, bmax),
        "brecha_min": bmin,
        "brecha_max": bmax,
    }

    # Top 3 anuncios
    top_buy = [
        {"price": a["price"], "min": a["min"], "max": a["max"]}
        for a in full_payload["binance"]["ads"][:3]
    ]
    top_sell = [
        {"price": a["price"], "min": a["min"], "max": a["max"]}
        for a in full_payload["binance_sell"]["ads"][:3]
    ]

    # Brecha actual (USDT BUY mediana vs BCV USD)
    brecha_now = None
    try:
        now_usdt = full_payload["binance"]["prices"]["median"]
        now_bcv = full_payload["bcv"]["usd_ves"]
        if now_usdt is not None and now_bcv not in (None, 0):
            brecha_now = ((now_usdt - now_bcv) / now_bcv) * 100.0
    except Exception:
        pass

    # Brecha promedio del día
    brecha_day_avg = None
    try:
        if day_agg and bcv_usd not in (None, 0):
            brecha_day_avg = ((day_agg["buy_avg"] - bcv_usd) / bcv_usd) * 100.0
    except Exception:
        pass

    return {
        "updated_at": full_payload["updated_at"],
        "bcv": full_payload["bcv"],   # incluye effective_date y date_label
        "binance_prices": {
            "buy_now": full_payload["binance"]["prices"],
            "sell_now": full_payload["binance_sell"]["prices"],
            "day_avg": day_agg,
        },
        "top_ads": {"buy": top_buy, "sell": top_sell},
        "history": hist,
        "change": change,
        "chart": chart,
        "warning": full_payload.get("warning"),
        "error": full_payload.get("error"),
        "brecha": {
            "now_pct": brecha_now,
            "day_avg_pct": brecha_day_avg,
        },
    }


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path not in ("/", "/data.json", "/summary.json"):
            self._send_json({"error": "Not found"}, status=404)
            return

        try:
            full_payload = build_full_payload()
            summary_payload = build_summary_payload(full_payload)

            CACHE["data"] = {"full": full_payload, "summary": summary_payload}
            CACHE["updated_at"] = full_payload["updated_at"]
            CACHE["error"] = None

            if self.path == "/summary.json":
                self._send_json(summary_payload, status=200)
            else:
                self._send_json(full_payload, status=200)

        except Exception as e:
            CACHE["error"] = f"{type(e).__name__}: {e}"
            if CACHE.get("data"):
                cached = (
                    CACHE["data"]["summary"]
                    if self.path == "/summary.json"
                    else CACHE["data"]["full"]
                )
                cached = dict(cached)
                cached["warning"] = "Mostrando cache por error al actualizar."
                cached["error"] = CACHE["error"]
                self._send_json(cached, status=200)
            else:
                self._send_json({"error": CACHE["error"]}, status=500)

    def log_message(self, format, *args):
        logging.info(f"[HTTP] {self.address_string()} - {format % args}")


def main():
    host = "127.0.0.1"
    port = 8000
    ensure_storage()
    logging.info(f"Servidor iniciado en http://{host}:{port}")
    logging.info(f"  Full:    http://{host}:{port}/data.json")
    logging.info(f"  Summary: http://{host}:{port}/summary.json")
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
