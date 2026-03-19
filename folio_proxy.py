#!/usr/bin/env python3
"""
FOLIO — Proxy local (Yahoo Finance + OpenFIGI)
===============================================
Lance ce script avant d'utiliser FOLIO.
Laisse ce terminal ouvert pendant la session.

Usage :
    python folio_proxy.py

Routes :
    /health           → ping
    /quote/<TICKER>   → cours Yahoo Finance (historique 6 mois)
    /search/<ISIN>    → résolution ISIN → ticker (OpenFIGI puis Yahoo)
"""

import json
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 7000

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
    "Cache-Control": "no-cache",
}

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


def resolve_isin_openfigi(isin):
    """
    Appelle l'API OpenFIGI (Bloomberg) — gratuite, pas de clé requise.
    Retourne une liste de correspondances {ticker, exchange, name, type}.
    """
    payload = json.dumps([{"idType": "ID_ISIN", "idValue": isin}]).encode()
    req = urllib.request.Request(
        OPENFIGI_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    if not data or not data[0].get("data"):
        return results

    # Map exchCode OpenFIGI → suffixe Yahoo Finance
    EXCH_MAP = {
        "FP":  ".PA",   # Euronext Paris
        "GY":  ".DE",   # XETRA
        "NA":  ".AS",   # Amsterdam
        "IM":  ".MI",   # Milan
        "SM":  ".MC",   # Madrid
        "BB":  ".BR",   # Bruxelles
        "SW":  ".SW",   # Suisse
        "LN":  ".L",    # Londres
        "AU":  ".AX",   # Australie
        "HK":  ".HK",   # Hong Kong
        "CN":  ".TO",   # Toronto
        "US":  "",      # USA (pas de suffixe)
        "UW":  "",      # NASDAQ
        "UN":  "",      # NYSE
        "UA":  "",      # AMEX
    }

    seen = set()
    for item in data[0]["data"]:
        raw_ticker = item.get("ticker", "")
        exch = item.get("exchCode", "")
        name = item.get("name", raw_ticker)
        sec_type = item.get("securityType", "")
        mic = item.get("marketSector", "")

        suffix = EXCH_MAP.get(exch, None)
        if suffix is None:
            continue  # exchange inconnu, on skip

        yahoo_ticker = raw_ticker.replace(" ", "-") + suffix

        # Déduplication
        key = yahoo_ticker.upper()
        if key in seen:
            continue
        seen.add(key)

        asset_type = "ETF" if "ETF" in sec_type or "ETF" in name.upper() else "Action"

        results.append({
            "ticker":   yahoo_ticker,
            "name":     name,
            "type":     asset_type,
            "exchange": exch,
            "currency": _currency_from_exch(exch),
            "source":   "OpenFIGI",
        })

    return results


def resolve_isin_yahoo(isin):
    """Fallback : recherche Yahoo Finance v1/search."""
    url = (
        f"https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={urllib.parse.quote(isin)}&quotesCount=5&newsCount=0"
    )
    req = urllib.request.Request(url, headers=YAHOO_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    results = []
    for q in (data.get("quotes") or []):
        if not q.get("symbol"):
            continue
        results.append({
            "ticker":   q["symbol"],
            "name":     q.get("longname") or q.get("shortname") or q["symbol"],
            "type":     "ETF" if q.get("quoteType") == "ETF" else "Action",
            "exchange": q.get("exchDisp", ""),
            "currency": q.get("currency", "EUR"),
            "source":   "Yahoo",
        })
    return results


def _currency_from_exch(exch):
    CURR = {
        "FP": "EUR", "GY": "EUR", "NA": "EUR", "IM": "EUR",
        "SM": "EUR", "BB": "EUR", "SW": "CHF", "LN": "GBP",
        "AU": "AUD", "HK": "HKD", "CN": "CAD",
        "US": "USD", "UW": "USD", "UN": "USD", "UA": "USD",
    }
    return CURR.get(exch, "EUR")


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        path = self.path.split("?")[0]
        print(f"  → {path}")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "port": PORT})
            return

        if self.path.startswith("/quote/"):
            ticker = urllib.parse.unquote(self.path[7:].split("?")[0].strip())
            self._fetch_yahoo_quote(ticker)
            return

        if self.path.startswith("/search/"):
            isin = urllib.parse.unquote(self.path[8:].split("?")[0].strip())
            self._resolve_isin(isin)
            return

        # Route : /stooq/<TICKER_STOOQ>  →  Stooq JSON (sans CORS côté serveur)
        if self.path.startswith("/stooq/"):
            parts = urllib.parse.unquote(self.path[7:])
            st = parts.split("?")[0].strip()
            self._fetch_stooq(st)
            return

        # Route : /stooq-csv/<TICKER>?d1=YYYYMMDD&d2=YYYYMMDD
        if self.path.startswith("/stooq-csv/"):
            raw = urllib.parse.unquote(self.path[11:])
            st  = raw.split("?")[0].strip()
            qs  = dict(p.split("=") for p in raw.split("?")[1].split("&")) if "?" in raw else {}
            d1  = qs.get("d1", "")
            d2  = qs.get("d2", "")
            self._fetch_stooq_csv(st, d1, d2)
            return

        self.send_response(404)
        self._cors()
        self.end_headers()

    def _fetch_stooq(self, st):
        url = f"https://stooq.com/q/l/?s={urllib.parse.quote(st)}&f=sd2ohlcv&h&e=json"
        try:
            req = urllib.request.Request(url, headers=YAHOO_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)}, status=502)

    def _fetch_stooq_csv(self, st, d1, d2):
        url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(st)}&d1={d1}&d2={d2}&i=d"
        try:
            req = urllib.request.Request(url, headers=YAHOO_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)}, status=502)

    def _resolve_isin(self, isin):
        """
        Résolution ISIN → ticker(s).
        1. OpenFIGI (Bloomberg) — source primaire fiable
        2. Yahoo search — fallback si OpenFIGI échoue
        Retourne format compatible avec le frontend FOLIO.
        """
        print(f"  [ISIN] Résolution de {isin}")
        results = []

        # --- Essai 1 : OpenFIGI ---
        try:
            results = resolve_isin_openfigi(isin)
            if results:
                print(f"    ✓ OpenFIGI : {results[0]['ticker']}")
        except Exception as e:
            print(f"    ✗ OpenFIGI erreur : {e}")

        # --- Essai 2 : Yahoo fallback ---
        if not results:
            try:
                results = resolve_isin_yahoo(isin)
                if results:
                    print(f"    ✓ Yahoo : {results[0]['ticker']}")
            except Exception as e:
                print(f"    ✗ Yahoo erreur : {e}")

        if not results:
            print(f"    ✗ Aucun résultat pour {isin}")

        # Format compatible avec l'attendu du frontend : {quotes: [...]}
        self._json({
            "quotes": [
                {
                    "symbol":    r["ticker"],
                    "longname":  r["name"],
                    "shortname": r["name"],
                    "quoteType": "ETF" if r["type"] == "ETF" else "EQUITY",
                    "exchDisp":  r["exchange"],
                    "currency":  r["currency"],
                    "_source":   r["source"],
                }
                for r in results
            ]
        })

    def _fetch_yahoo_quote(self, ticker):
        """Récupère l'historique de cours Yahoo Finance (6 mois, quotidien)."""
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(ticker)}"
            f"?interval=1d&range=6mo&includePrePost=false"
        )
        try:
            req = urllib.request.Request(url, headers=YAHOO_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)}, status=502)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")


def main():
    print()
    print("╔══════════════════════════════════════════╗")
    print("║   FOLIO — Proxy local v2                 ║")
    print(f"║   En écoute sur http://localhost:{PORT}    ║")
    print("║   Sources : Yahoo Finance + OpenFIGI     ║")
    print("║   Ctrl+C pour arrêter                    ║")
    print("╚══════════════════════════════════════════╝")
    print()
    print("Activité :")

    server = HTTPServer(("localhost", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Proxy arrêté.")


if __name__ == "__main__":
    main()
