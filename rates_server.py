from http.server import BaseHTTPRequestHandler, HTTPServer
import datetime as dt
import json
import logging
import os
import csv
import zoneinfo
from statistics import median, mean

import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)

BCV_URL = "https://www.bcv.org.ve"
BINANCE_P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

VET = zoneinfo.ZoneInfo("America/Caracas")

SAMPLE_INTERVAL_SECONDS = 300  # 5 minutos

# Ventana horaria para consultar el BCV (hora Venezuela)
# A las 6-7AM el BCV ya tiene publicada la tasa correcta del día
BCV_FETCH_HOUR_START = 6
BCV_FETCH_HOUR_END   = 19  # hasta las 7PM seguimos actualizando si no hay dato del día

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

HISTORY_CSV = os.path.join(DATA_DIR, "rates_history.csv")
SAMPLES_CSV = os.path.join(DATA_DIR, "binance_samples.csv")

CACHE = {"data": None, "updated_at": None, "error": None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_vet() -> dt.datetime:
    return dt.datetime.now(VET)


def _to_float_ves(s: str) -> float:
    s = s.strip().replace(".", "").replace(",", ".")
    return float(s)


def normalize_date_str(s: str) -> str:
    s = (s or "").strip()
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{y:04d}-{m:02d}-{d:02d}"
    return s


def dedupe_and_sort_history(rows: list) -> list:
    dedup = {}
    for r in rows:
        key = r.get("date")
        if key:
            dedup[key] = r
    out = list(dedup.values())
    out.sort(key=lambda x: x.get("date", ""))
    return out


def get_last_bcv_rate() -> dict | None:
    """
    Lee el CSV y retorna la tasa BCV más reciente disponible.
    Útil para usar cuando no estamos en la ventana de consulta al BCV.
    """
    if not os.path.exists(HISTORY_CSV):
        return None
    rows = []
    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["date"] = normalize_date_str(row.get("date", ""))
            rows.append(row)
    rows = dedupe_and_sort_history(rows)
    if not rows:
        return None
    last = rows[-1]
    try:
        return {
            "ves": float(last["bcv_usd_ves"]),
            "eur": float(last["bcv_eur_ves"]),
        }
    except Exception:
        return None


def should_fetch_bcv(now: dt.datetime) -> bool:
    """
    Retorna True solo si estamos en la ventana horaria válida para
    consultar el BCV (6AM - 7PM VET en días hábiles L-V).
    Fuera de esa ventana usamos la última tasa conocida del CSV.
    """
    if now.weekday() >= 5:  # fin de semana
        return False
    return BCV_FETCH_HOUR_START <= now.hour < BCV_FETCH_HOUR_END


# ---------------------------------------------------------------------------
# Almacenamiento
# ---------------------------------------------------------------------------

def ensure_storage():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "date", "bcv_usd_ves", "bcv_eur_ves",
                "usdt_buy_avg", "usdt_sell_avg", "samples_count",
                "first_sample_at", "last_sample_at",
            ])

    if not os.path.exists(SAMPLES_CSV):
        with open(SAMPLES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "date", "usdt_buy_median", "usdt_sell_median"])


# ---------------------------------------------------------------------------
# Fuentes externas
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
    return {"ves": _to_float_ves(raw), "source": BCV_URL}


def fetch_binance_ads_first_100(fiat="VES", asset="USDT", trade_type="BUY",
                                 limit=100, rows=20, timeout=15):
    ads, page = [], 1
    while len(ads) < limit:
        body = {"fiat": fiat, "page": page, "rows": rows,
                "tradeType": trade_type, "asset": asset}
        r = requests.post(BINANCE_P2P_URL, json=body, timeout=timeout,
                          headers={"accept": "application/json",
                                   "content-type": "application/json",
                                   "user-agent": "Mozilla/5.0"})
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "000000":
            raise RuntimeError(f"Binance P2P code={j.get('code')}")
        data = j.get("data", [])
        if not isinstance(data, list) or len(data) == 0:
            break
        for item in data:
            adv = item.get("adv", {}) or {}
            ads.append({
                "price": float(adv.get("price", 0) or 0),
                "min":   float(adv.get("minSingleTransAmount", 0) or 0),
                "max":   float(adv.get("maxSingleTransAmount", 0) or 0),
            })
            if len(ads) >= limit:
                break
        page += 1
        if page > 10:
            break

    prices = [a["price"] for a in ads if a["price"] > 0]
    stats = {
        "count":   len(prices),
        "median":  median(prices) if prices else None,
        "average": mean(prices)   if prices else None,
        "min":     min(prices)    if prices else None,
        "max":     max(prices)    if prices else None,
    }
    return {"trade_type": trade_type, "prices": stats,
            "ads": ads, "source": BINANCE_P2P_URL}


# ---------------------------------------------------------------------------
# Muestras intradía Binance
# ---------------------------------------------------------------------------

def read_last_sample_ts():
    if not os.path.exists(SAMPLES_CSV):
        return None
    last = None
    with open(SAMPLES_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
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
        now_utc  = now.astimezone(dt.timezone.utc)
        last_utc = last_ts.astimezone(dt.timezone.utc)
        if (now_utc - last_utc).total_seconds() < SAMPLE_INTERVAL_SECONDS:
            return
    date_vet = now.astimezone(VET).date().isoformat()
    with open(SAMPLES_CSV, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([now.isoformat(timespec="seconds"), date_vet,
                                 buy_median, sell_median])


def aggregate_day(date_str: str):
    samples_buy, samples_sell = [], []
    first_ts = last_ts = None
    if not os.path.exists(SAMPLES_CSV):
        return None
    with open(SAMPLES_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["date"] != date_str:
                continue
            try:
                ts = dt.datetime.fromisoformat(row["ts"])
                b  = float(row["usdt_buy_median"])
                s  = float(row["usdt_sell_median"])
            except Exception:
                continue
            samples_buy.append(b)
            samples_sell.append(s)
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    if not samples_buy:
        return None
    return {
        "count":           len(samples_buy),
        "buy_avg":         mean(samples_buy),
        "sell_avg":        mean(samples_sell),
        "buy_min":         min(samples_buy),
        "buy_max":         max(samples_buy),
        "sell_min":        min(samples_sell),
        "sell_max":        max(samples_sell),
        "first_sample_at": first_ts.isoformat(timespec="seconds"),
        "last_sample_at":  last_ts.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Historial diario
# ---------------------------------------------------------------------------

def upsert_daily_row(today: str, bcv_usd: float, bcv_eur: float, day_agg: dict):
    ensure_storage()
    rows, found = [], False

    with open(HISTORY_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["date"] == today:
                found = True
                row["bcv_usd_ves"]    = str(bcv_usd)
                row["bcv_eur_ves"]    = str(bcv_eur)
                row["usdt_buy_avg"]   = str(day_agg["buy_avg"])
                row["usdt_sell_avg"]  = str(day_agg["sell_avg"])
                row["samples_count"]  = str(day_agg["count"])
                row["first_sample_at"] = day_agg["first_sample_at"]
                row["last_sample_at"]  = day_agg["last_sample_at"]
                logging.info(f"[BCV] Actualizado {today}: USD={bcv_usd}")
            rows.append(row)

    if not found:
        rows.append({
            "date":            today,
            "bcv_usd_ves":     str(bcv_usd),
            "bcv_eur_ves":     str(bcv_eur),
            "usdt_buy_avg":    str(day_agg["buy_avg"]),
            "usdt_sell_avg":   str(day_agg["sell_avg"]),
            "samples_count":   str(day_agg["count"]),
            "first_sample_at": day_agg["first_sample_at"],
            "last_sample_at":  day_agg["last_sample_at"],
        })
        logging.info(f"[BCV] Nueva fila {today}: USD={bcv_usd}")

    all_keys = [k for k in list(rows[0].keys()) if k != "bcv_locked"]
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
        for row in csv.DictReader(f):
            row["date"] = normalize_date_str(row.get("date", ""))
            out.append(row)
    return dedupe_and_sort_history(out)[-n:]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def pct_change(today: float, yesterday: float):
    if today is None or yesterday is None or yesterday == 0:
        return None
    return ((today - yesterday) / yesterday) * 100.0


def pct_change_vs_last_different(current: float, history: list, field: str):
    """
    Calcula el cambio % vs la última fila con valor DIFERENTE al actual.
    """
    if current is None:
        return None
    for row in reversed(history):
        try:
            val = float(row[field])
            if round(val, 4) != round(current, 4):
                return pct_change(current, val)
        except Exception:
            continue
    return None


def normalize_series(values, vmin, vmax):
    if vmin is None or vmax is None or vmax == vmin:
        return [50 for _ in values]
    return [
        None if v is None
        else int(round(((v - vmin) / (vmax - vmin)) * 100))
        for v in values
    ]


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def build_full_payload() -> dict:
    now = now_vet()

    # Consultar BCV solo en ventana horaria válida (6AM-7PM VET, L-V)
    # Fuera de esa ventana usamos la última tasa conocida del CSV
    if should_fetch_bcv(now):
        usd_data = fetch_bcv_rate("dolar")
        eur_data = fetch_bcv_rate("euro")
        usd_ves  = usd_data["ves"]
        eur_ves  = eur_data["ves"]
        logging.info(f"[BCV] Consultado en vivo: USD={usd_ves}")
    else:
        cached = get_last_bcv_rate()
        if cached:
            usd_ves = cached["ves"]
            eur_ves = cached["eur"]
            logging.info(f"[BCV] Fuera de ventana horaria, usando último valor del CSV: USD={usd_ves}")
        else:
            # Fallback: consultar de todas formas si no hay dato
            usd_data = fetch_bcv_rate("dolar")
            eur_data = fetch_bcv_rate("euro")
            usd_ves  = usd_data["ves"]
            eur_ves  = eur_data["ves"]
            logging.info(f"[BCV] Sin dato en CSV, consultando de todas formas: USD={usd_ves}")

    buy  = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="BUY")
    sell = fetch_binance_ads_first_100(limit=100, rows=20, trade_type="SELL")

    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "bcv": {
            "usd_ves": usd_ves,
            "eur_ves": eur_ves,
            "source":  BCV_URL,
        },
        "binance":      buy,
        "binance_sell": sell,
        "warning": None,
        "error":   None,
    }


def build_summary_payload(full_payload: dict, history_days=30) -> dict:
    now       = now_vet()
    today_str = now.date().isoformat()

    bcv_usd = full_payload["bcv"]["usd_ves"]
    bcv_eur = full_payload["bcv"]["eur_ves"]
    buy_med  = full_payload["binance"]["prices"]["median"]
    sell_med = full_payload["binance_sell"]["prices"]["median"]

    record_sample_if_due(now, buy_med, sell_med)
    day_agg = aggregate_day(today_str)

    if day_agg:
        upsert_daily_row(today_str, bcv_usd, bcv_eur, day_agg)

    hist = read_history_last_n(history_days)

    change = {"bcv_usd": {"pct": None}, "usdt_buy_avg": {"pct": None}}
    if len(hist) >= 2:
        try:
            change["bcv_usd"]["pct"] = pct_change_vs_last_different(
                bcv_usd, hist[:-1], "bcv_usd_ves")
        except Exception:
            pass
        try:
            change["usdt_buy_avg"]["pct"] = pct_change(
                float(hist[-1]["usdt_buy_avg"]),
                float(hist[-2]["usdt_buy_avg"]))
        except Exception:
            pass

    dates, bcv_series, usdt_series = [], [], []
    for r in hist:
        dates.append(r["date"])
        try:
            bcv_series.append(float(r["bcv_usd_ves"]))
        except Exception:
            bcv_series.append(None)
        try:
            usdt_series.append(float(r["usdt_buy_avg"]))
        except Exception:
            usdt_series.append(None)

    all_vals = [v for v in bcv_series + usdt_series if v is not None]
    vmin_raw = min(all_vals) if all_vals else None
    vmax_raw = max(all_vals) if all_vals else None

    AXIS_PAD = 0.03
    if vmin_raw is not None and vmax_raw is not None:
        vrange = vmax_raw - vmin_raw
        vmin   = vmin_raw - vrange * AXIS_PAD
        vmax   = vmax_raw + vrange * AXIS_PAD
    else:
        vmin = vmax = None

    brecha_series = []
    for b, u in zip(bcv_series, usdt_series):
        if b is None or u is None or b == 0:
            brecha_series.append(None)
        else:
            brecha_series.append(((u - b) / b) * 100.0)

    brecha_vals = [v for v in brecha_series if v is not None]
    bmin_raw = min(brecha_vals) if brecha_vals else None
    bmax_raw = max(brecha_vals) if brecha_vals else None
    if bmin_raw is not None and bmax_raw is not None:
        brange = bmax_raw - bmin_raw
        bmin   = bmin_raw - brange * AXIS_PAD
        bmax   = bmax_raw + brange * AXIS_PAD
    else:
        bmin = bmax = None

    chart = {
        "dates":       dates,
        "bcv_norm":    normalize_series(bcv_series,    vmin, vmax),
        "usdt_norm":   normalize_series(usdt_series,   vmin, vmax),
        "vmin":        vmin,
        "vmax":        vmax,
        "brecha_norm": normalize_series(brecha_series, bmin, bmax),
        "brecha_min":  bmin,
        "brecha_max":  bmax,
    }

    top_buy  = [{"price": a["price"], "min": a["min"], "max": a["max"]}
                for a in full_payload["binance"]["ads"][:3]]
    top_sell = [{"price": a["price"], "min": a["min"], "max": a["max"]}
                for a in full_payload["binance_sell"]["ads"][:3]]

    brecha_now = brecha_day_avg = None
    try:
        now_usdt = full_payload["binance"]["prices"]["median"]
        if now_usdt and bcv_usd:
            brecha_now = ((now_usdt - bcv_usd) / bcv_usd) * 100.0
    except Exception:
        pass
    try:
        if day_agg and bcv_usd:
            brecha_day_avg = ((day_agg["buy_avg"] - bcv_usd) / bcv_usd) * 100.0
    except Exception:
        pass

    return {
        "updated_at": full_payload["updated_at"],
        "bcv":        full_payload["bcv"],
        "binance_prices": {
            "buy_now":  full_payload["binance"]["prices"],
            "sell_now": full_payload["binance_sell"]["prices"],
            "day_avg":  day_agg,
        },
        "top_ads":  {"buy": top_buy, "sell": top_sell},
        "history":  hist,
        "change":   change,
        "chart":    chart,
        "warning":  full_payload.get("warning"),
        "error":    full_payload.get("error"),
        "brecha":   {"now_pct": brecha_now, "day_avg_pct": brecha_day_avg},
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
            full    = build_full_payload()
            summary = build_summary_payload(full)
            CACHE["data"]       = {"full": full, "summary": summary}
            CACHE["updated_at"] = full["updated_at"]
            CACHE["error"]      = None
            self._send_json(summary if self.path == "/summary.json" else full)
        except Exception as e:
            CACHE["error"] = f"{type(e).__name__}: {e}"
            if CACHE.get("data"):
                cached = dict(CACHE["data"]["summary" if self.path == "/summary.json" else "full"])
                cached["warning"] = "Cache por error al actualizar."
                cached["error"]   = CACHE["error"]
                self._send_json(cached)
            else:
                self._send_json({"error": CACHE["error"]}, status=500)

    def log_message(self, format, *args):
        logging.info(f"[HTTP] {self.address_string()} - {format % args}")


def main():
    host, port = "127.0.0.1", 8000
    ensure_storage()
    logging.info(f"Servidor en http://{host}:{port}")
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()