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

# cada cuánto guardamos una muestra Binance (para no llenar el disco ni “spamear”)
SAMPLE_INTERVAL_SECONDS = 300  # 5 minutos

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

HISTORY_CSV = os.path.join(DATA_DIR, "rates_history.csv")          # 1 fila por día (promedios)
SAMPLES_CSV = os.path.join(DATA_DIR, "binance_samples.csv")        # muchas filas por día (muestras)

CACHE = {"data": None, "updated_at": None, "error": None}


def _to_float_ves(s: str) -> float:
    s = s.strip().replace(".", "").replace(",", ".")
    return float(s)


def ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # 1 fila por día (promedios diarios)
            w.writerow([
                "date",
                "bcv_usd_ves",
                "bcv_eur_ves",
                "usdt_buy_avg",
                "usdt_sell_avg",
                "samples_count",
                "first_sample_at",
                "last_sample_at",
            ])

    if not os.path.exists(SAMPLES_CSV):
        with open(SAMPLES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # muestras intradía
            w.writerow(["ts", "date", "usdt_buy_median", "usdt_sell_median"])

def normalize_date_str(s: str) -> str:
    s = (s or "").strip()
    # Convierte "10/3/2026" (día/mes/año) a "2026-03-10"
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            d = int(parts[0])
            m = int(parts[1])
            y = int(parts[2])
            return f"{y:04d}-{m:02d}-{d:02d}"
    return s


def dedupe_and_sort_history(rows: list) -> list:
    # elimina duplicados por date quedándose con el último
    dedup = {}
    for r in rows:
        key = r.get("date")
        if key:
            dedup[key] = r
    out = list(dedup.values())
    out.sort(key=lambda x: x.get("date", ""))
    return out



def fetch_bcv_rate(currency_id: str, timeout=15) -> dict:
    r = requests.get(BCV_URL, timeout=timeout, verify=False, headers={"User-Agent": "Mozilla/5.0"})
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
            raise RuntimeError(f"Binance P2P respondió code={j.get('code')} message={j.get('message')}")

        data = j.get("data", [])
        if not isinstance(data, list) or len(data) == 0:
            break

        for item in data:
            adv = item.get("adv", {}) or {}
            ads.append(
                {
                    "price": float(adv.get("price", 0) or 0),
                    "min": float(adv.get("minSingleTransAmount", 0) or 0),
                    "max": float(adv.get("maxSingleTransAmount", 0) or 0),
                }
            )
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
        w.writerow([now.isoformat(timespec="seconds"), now.date().isoformat(), buy_median, sell_median])


def aggregate_day(date_str: str):
    """
    Devuelve promedios del día usando las muestras intradía.
    """
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


def upsert_daily_row(date_str: str, bcv_usd: float, bcv_eur: float, day_agg: dict):
    """
    Mantiene 1 fila por día en rates_history.csv, y la va actualizando
    para que quede el promedio final del día.
    """
    ensure_storage()

    rows = []
    found = False

    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["date"] == date_str:
                found = True
                row["bcv_usd_ves"] = str(bcv_usd)
                row["bcv_eur_ves"] = str(bcv_eur)
                row["usdt_buy_avg"] = str(day_agg["buy_avg"])
                row["usdt_sell_avg"] = str(day_agg["sell_avg"])
                row["samples_count"] = str(day_agg["count"])
                row["first_sample_at"] = day_agg["first_sample_at"]
                row["last_sample_at"] = day_agg["last_sample_at"]
            rows.append(row)

    if not found:
        rows.append({
            "date": date_str,
            "bcv_usd_ves": str(bcv_usd),
            "bcv_eur_ves": str(bcv_eur),
            "usdt_buy_avg": str(day_agg["buy_avg"]),
            "usdt_sell_avg": str(day_agg["sell_avg"]),
            "samples_count": str(day_agg["count"]),
            "first_sample_at": day_agg["first_sample_at"],
            "last_sample_at": day_agg["last_sample_at"],
        })

    with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def read_history_last_n(n=30):
    if not os.path.exists(HISTORY_CSV):
        return []

    out = []
    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            # normaliza fecha aunque venga con /
            row["date"] = normalize_date_str(row.get("date"))
            out.append(row)

    out = dedupe_and_sort_history(out)
    return out[-n:]



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


def build_full_payload() -> dict:
    now = dt.datetime.now().astimezone()

    usd = fetch_bcv_rate("dolar")
    eur = fetch_bcv_rate("euro")

    buy = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="BUY")
    sell = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="SELL")

    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "bcv": {"usd_ves": usd["ves"], "eur_ves": eur["ves"], "source": BCV_URL},
        "binance": buy,
        "binance_sell": sell,
        "warning": None,
        "error": None,
    }


def build_summary_payload(full_payload: dict, history_days=30) -> dict:
    now = dt.datetime.now().astimezone()
    date_str = now.date().isoformat()

    bcv_usd = full_payload["bcv"]["usd_ves"]
    bcv_eur = full_payload["bcv"]["eur_ves"]

    buy_med = full_payload["binance"]["prices"]["median"]
    sell_med = full_payload["binance_sell"]["prices"]["median"]

    # 1) Guardar muestra intradía (throttle por SAMPLE_INTERVAL_SECONDS)
    record_sample_if_due(now, buy_med, sell_med)

    # 2) Calcular agregado del día (promedio diario)
    day_agg = aggregate_day(date_str)

    # 3) Actualizar fila diaria (promedio del día “corriendo”)
    if day_agg:
        upsert_daily_row(date_str, bcv_usd, bcv_eur, day_agg)

    # 4) Leer histórico diario (últimos N días)
    hist = read_history_last_n(history_days)

    # Cambios vs ayer (usando promedio diario ya guardado)
    change = {
        "bcv_usd": {"pct": None},
        "usdt_buy_avg": {"pct": None},
    }
    if len(hist) >= 2:
        y = hist[-2]
        t = hist[-1]
        try:
            change["bcv_usd"]["pct"] = pct_change(float(t["bcv_usd_ves"]), float(y["bcv_usd_ves"]))
        except Exception:
            pass
        try:
            change["usdt_buy_avg"]["pct"] = pct_change(float(t["usdt_buy_avg"]), float(y["usdt_buy_avg"]))
        except Exception:
            pass

        # Chart series (usa promedios diarios)
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

    # Eje izquierdo (Bs): escala común BCV + USDT
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
    brecha_norm = normalize_series(brecha_series, bmin, bmax)

    chart = {
        "dates": dates,
        "bcv_norm": normalize_series(bcv_series, vmin, vmax),
        "usdt_norm": normalize_series(usdt_series, vmin, vmax),
        "vmin": vmin,
        "vmax": vmax,

        "brecha_norm": brecha_norm,
        "brecha_min": bmin,
        "brecha_max": bmax,
    }


    # Top anuncios (3)
    top_buy = [{"price": a["price"], "min": a["min"], "max": a["max"]} for a in full_payload["binance"]["ads"][:3]]
    top_sell = [{"price": a["price"], "min": a["min"], "max": a["max"]} for a in full_payload["binance_sell"]["ads"][:3]]

    # Brecha ahora (USDT BUY mediana actual vs BCV USD/VES)
    brecha_now = None
    try:
        now_usdt = full_payload["binance"]["prices"]["median"]
        now_bcv = full_payload["bcv"]["usd_ves"]
        if now_usdt is not None and now_bcv not in (None, 0):
            brecha_now = ((now_usdt - now_bcv) / now_bcv) * 100.0
    except Exception:
        pass

    # Brecha promedio del día (si existe day_agg)
    brecha_day_avg = None
    try:
        if day_agg and now_bcv not in (None, 0):
            brecha_day_avg = ((day_agg["buy_avg"] - now_bcv) / now_bcv) * 100.0
    except Exception:
        pass

    return {
        "updated_at": full_payload["updated_at"],
        "bcv": full_payload["bcv"],
        "binance_prices": {
            "buy_now": full_payload["binance"]["prices"],          # instante
            "sell_now": full_payload["binance_sell"]["prices"],    # instante
            "day_avg": day_agg,                                    # promedio del día
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
                cached = CACHE["data"]["summary"] if self.path == "/summary.json" else CACHE["data"]["full"]
                cached = dict(cached)
                cached["warning"] = "Mostrando cache por error al actualizar."
                cached["error"] = CACHE["error"]
                self._send_json(cached, status=200)
            else:
                self._send_json({"error": CACHE["error"]}, status=500)


def main():
    host = "127.0.0.1"
    port = 8000
    ensure_storage()
    print(f"Servidor JSON full:    http://{host}:{port}/data.json")
    print(f"Servidor JSON summary: http://{host}:{port}/summary.json")
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()