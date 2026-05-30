"""
Daily Market Scanner — Stocks + Crypto
=======================================
Scans the S&P 500 + Nasdaq 100 for momentum stock setups, and the top 50
cryptocurrencies for momentum crypto setups, ranking the top 10 of each.

Stock filter:
  - Price within 2% of 52-week high
  - 21 EMA > 200 SMA (trend confirmation)
  - Today's volume >= 1.3x 30-day average
  - Today's close > yesterday's close
  Rank: (volume surge) * (% above 200 SMA)

Crypto filter:
  - Top 50 by market cap (stablecoins excluded)
  - RSI(14) between 55 and 75 (momentum without mean-reversion zone)
  - 24h volume >= 1.5x 7-day average
  - 24h price change > 0
  Rank: (volume surge) * (RSI - 50)
  Funding rate (Kraken Futures perpetuals) shown as context, not filtered.

Data sources (all free, no API keys):
  - yfinance: stock price/volume
  - Wikipedia: S&P 500 and Nasdaq 100 universe (with hardcoded fallback)
  - CoinGecko: crypto markets + historical prices
  - Kraken Futures: perpetual funding rates

Usage:
    python market_scanner.py
    python market_scanner.py --stocks-only
    python market_scanner.py --crypto-only
    python market_scanner.py --top 20              # top 20 instead of 10
    python market_scanner.py --output scan.csv     # save results

Integration:
    The scanner returns DataFrames via scan_stocks() and scan_crypto() so you
    can import into your existing platform:

        from market_scanner import scan_stocks, scan_crypto
        stock_df = scan_stocks(top_n=10)
        crypto_df = scan_crypto(top_n=10)
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from dataclasses import dataclass
from io import StringIO
from typing import Optional

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Stock filter parameters
STOCK_PCT_OF_52W_HIGH = 0.98      # within 2% of 52w high
STOCK_VOL_SURGE_MIN   = 1.30      # 30% above 30d avg volume
STOCK_HISTORY_DAYS    = "1y"      # yfinance period string

# Crypto filter parameters
CRYPTO_RSI_MIN        = 55        # momentum floor
CRYPTO_RSI_MAX        = 75        # cap before mean-reversion zone
CRYPTO_VOL_SURGE_MIN  = 1.50      # 50% above 7d avg volume

# Stablecoins and wrapped tokens to exclude from crypto universe
STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX", "LUSD",
    "USDE", "FDUSD", "PYUSD", "USDD", "GUSD", "SUSD", "USD1",
}
WRAPPED_SYMBOLS = {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBETH", "RETH"}
CRYPTO_EXCLUDE = STABLECOIN_SYMBOLS | WRAPPED_SYMBOLS

# HTTP
USER_AGENT = "Mozilla/5.0 (market-scanner/1.0)"
HTTP_TIMEOUT = 20

# CoinGecko rate limit: free tier ~30 calls/min. Space coin queries out.
COINGECKO_DELAY = 2.5      # seconds between per-coin history calls (conservative)
COINGECKO_MAX_RETRIES = 3  # retries on 429 with exponential backoff

# Hardcoded fallback universe: large-cap liquid names spanning S&P/Nasdaq.
# Used if Wikipedia scraping fails (e.g., from a sandboxed environment).
FALLBACK_TICKERS = [
    "AAPL","MSFT","NVDA","GOOGL","GOOG","AMZN","META","TSLA","AVGO","LLY",
    "JPM","V","UNH","XOM","WMT","MA","JNJ","PG","HD","COST","ORCL","ABBV",
    "CVX","MRK","BAC","KO","PEP","NFLX","TMO","ADBE","CSCO","CRM","AMD",
    "ABT","LIN","WFC","DIS","ACN","MCD","TXN","DHR","INTU","VZ","NEE","PM",
    "INTC","AMGN","QCOM","IBM","CAT","GS","PFE","NOW","UNP","T","LOW","SPGI",
    "HON","NKE","AXP","RTX","UPS","BLK","BKNG","SCHW","BA","MDT","DE","GE",
    "PLD","ELV","SBUX","TJX","MDLZ","LMT","ADP","GILD","MMC","ISRG","CVS",
    "C","SYK","REGN","ZTS","VRTX","CB","ADI","AMAT","MU","LRCX","KLAC",
    "PANW","ANET","MRNA","MELI","PYPL","SNOW","CRWD","SHOP","ABNB","UBER",
    "COIN","PLTR","SMCI","ARM","APP","ASML","TSM","BIDU","JD","PDD","BABA",
]


# -----------------------------------------------------------------------------
# Ticker universe
# -----------------------------------------------------------------------------

def fetch_stock_universe() -> list[str]:
    """Return the combined S&P 500 + Nasdaq 100 universe.

    Tries Wikipedia first; falls back to a hardcoded list if the request fails.
    The fallback gives ~130 liquid large caps which is plenty to find setups.
    """
    try:
        headers = {"User-Agent": USER_AGENT}
        sp500_html = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers=headers, timeout=HTTP_TIMEOUT,
        ).text
        sp500 = pd.read_html(StringIO(sp500_html))[0]
        sp500_tickers = sp500["Symbol"].astype(str).tolist()

        ndx_html = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers=headers, timeout=HTTP_TIMEOUT,
        ).text
        ndx_tables = pd.read_html(StringIO(ndx_html))
        ndx_tickers: list[str] = []
        for t in ndx_tables:
            cols = [str(c) for c in t.columns]
            sym_col = next((c for c in ("Ticker", "Symbol") if c in cols), None)
            if sym_col and 80 <= len(t) <= 110:
                ndx_tickers = t[sym_col].astype(str).tolist()
                break

        combined = sorted(set(sp500_tickers) | set(ndx_tickers))
        # yfinance wants dots replaced with dashes (BRK.B -> BRK-B)
        combined = [s.replace(".", "-").strip() for s in combined if s and s != "nan"]
        print(f"Universe: {len(combined)} tickers from Wikipedia", file=sys.stderr)
        return combined
    except Exception as e:
        print(f"Wikipedia fetch failed ({e}); using fallback universe "
              f"of {len(FALLBACK_TICKERS)} large caps.", file=sys.stderr)
        return FALLBACK_TICKERS.copy()


# -----------------------------------------------------------------------------
# Technical indicators
# -----------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style RSI. Returns NaN for the first `period` bars."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def coingecko_get(url: str, params: dict | None = None) -> dict | list | None:
    """GET a CoinGecko endpoint with retry/backoff on 429 rate-limit responses.

    Returns parsed JSON on success, None if all retries exhausted.
    """
    delay = 30
    for attempt in range(COINGECKO_MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                if attempt < COINGECKO_MAX_RETRIES:
                    print(f"  CoinGecko rate-limited (429); waiting {delay}s "
                          f"before retry ({attempt + 1}/{COINGECKO_MAX_RETRIES})...",
                          file=sys.stderr)
                    time.sleep(delay)
                    delay *= 2  # exponential backoff
                    continue
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < COINGECKO_MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            print(f"  CoinGecko request failed after retries: {e}", file=sys.stderr)
            return None
    return None


# -----------------------------------------------------------------------------
# Stock scanner
# -----------------------------------------------------------------------------

def scan_stocks(top_n: int = 10, tickers: Optional[list[str]] = None) -> pd.DataFrame:
    """Run the stock scan. Returns a DataFrame ranked by composite score."""
    import yfinance as yf  # deferred import: only load if stock scan runs

    if tickers is None:
        tickers = fetch_stock_universe()

    print(f"Downloading daily bars for {len(tickers)} tickers...", file=sys.stderr)
    # yfinance can bulk-download; threaded behind the scenes. ~30-60s for 500 tickers.
    data = yf.download(
        tickers, period=STOCK_HISTORY_DAYS, interval="1d",
        progress=False, auto_adjust=True, group_by="ticker", threads=True,
    )

    results: list[dict] = []
    for t in tickers:
        try:
            # yfinance returns a multi-index when group_by="ticker"; single ticker
            # downloads return a flat frame — handle both.
            df = data[t] if t in data.columns.get_level_values(0) else data
            df = df.dropna()
            if len(df) < 200:  # need 200 bars for SMA200
                continue

            close = df["Close"]
            vol = df["Volume"]
            high = df["High"]

            # Core metrics
            last_close   = float(close.iloc[-1])
            prev_close   = float(close.iloc[-2])
            high_52w     = float(high.tail(252).max())
            ema21        = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
            sma200       = float(close.rolling(200).mean().iloc[-1])
            avg_vol_30   = float(vol.tail(30).mean())
            last_vol     = float(vol.iloc[-1])
            vol_surge    = last_vol / avg_vol_30 if avg_vol_30 > 0 else 0.0
            rsi_14       = float(rsi(close).iloc[-1])

            # Filter conditions
            near_52w  = last_close >= high_52w * STOCK_PCT_OF_52W_HIGH
            trend_ok  = ema21 > sma200
            vol_ok    = vol_surge >= STOCK_VOL_SURGE_MIN
            price_up  = last_close > prev_close

            if not (near_52w and trend_ok and vol_ok and price_up):
                continue

            pct_above_sma = (last_close - sma200) / sma200 * 100
            pct_from_52w  = (last_close / high_52w - 1) * 100
            # Composite: reward both fresh volume AND distance above long-term trend
            score = vol_surge * max(pct_above_sma, 0)

            results.append({
                "Ticker": t,
                "Price": round(last_close, 2),
                "Vol Change": round(vol_surge, 2),  # multiplier (e.g., 1.85 = +85%)
                "RSI": round(rsi_14, 1),
                "% from 52W High": round(pct_from_52w, 2),
                "% vs 200SMA": round(pct_above_sma, 1),
                "Score": round(score, 2),
                "Bullish Reason": (
                    f"Within {abs(pct_from_52w):.1f}% of 52W high on "
                    f"{vol_surge:.1f}x avg volume, trending {pct_above_sma:.0f}% "
                    "above 200-day MA."
                ),
            })
        except Exception:
            # Silently skip problem tickers (delisted, bad data, etc.)
            continue

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results).sort_values("Score", ascending=False).head(top_n)
    return out.reset_index(drop=True)


# -----------------------------------------------------------------------------
# Crypto scanner
# -----------------------------------------------------------------------------

def fetch_kraken_funding_rates() -> dict[str, float]:
    """Return {symbol: funding_rate_pct} for Kraken perpetuals.

    Kraken Futures returns `fundingRate` in absolute price units per contract,
    not a percentage. Normalize to % by dividing by mark price.
    """
    try:
        r = requests.get(
            "https://futures.kraken.com/derivatives/api/v3/tickers",
            timeout=HTTP_TIMEOUT,
        )
        tickers = r.json().get("tickers", [])
    except Exception as e:
        print(f"Kraken funding fetch failed: {e}", file=sys.stderr)
        return {}

    out: dict[str, float] = {}
    for t in tickers:
        sym = t.get("symbol", "")
        # PF_ = USD-margined perpetual (most relevant). Filter to USD quote.
        if not (sym.startswith("PF_") and sym.endswith("USD")):
            continue
        base = sym[3:-3]
        if base == "XBT":       # Kraken's symbol for Bitcoin
            base = "BTC"
        fr = t.get("fundingRate")
        mark = t.get("markPrice")
        if fr is None or mark is None or mark == 0:
            continue
        # Convert to percentage per funding interval
        out[base] = float(fr) / float(mark) * 100
    return out


def _coingecko_get(url: str, params: dict, max_retries: int = 3) -> Optional[dict | list]:
    """GET from CoinGecko with 429 backoff. Returns parsed JSON or None on failure."""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                # Rate limited — back off exponentially before retry
                wait = 30 * (attempt + 1)
                print(f"  CoinGecko rate-limited (429); waiting {wait}s before retry "
                      f"({attempt + 1}/{max_retries})...", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            payload = r.json()
            # CoinGecko returns {'status': {...}} on errors even with 200 sometimes
            if isinstance(payload, dict) and "status" in payload and "error_code" in payload.get("status", {}):
                print(f"  CoinGecko error: {payload['status']}", file=sys.stderr)
                return None
            return payload
        except Exception as e:
            print(f"  CoinGecko request failed: {e}", file=sys.stderr)
            if attempt < max_retries - 1:
                time.sleep(10)
    return None


def scan_crypto(top_n: int = 10) -> pd.DataFrame:
    """Run the crypto scan. Returns a DataFrame ranked by composite score."""
    # 1) Top 50 markets snapshot
    markets = _coingecko_get(
        "https://api.coingecko.com/api/v3/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 50,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        },
    )
    if not markets or not isinstance(markets, list):
        print("Crypto scan aborted: could not fetch market data from CoinGecko.",
              file=sys.stderr)
        return pd.DataFrame()

    # 2) Funding rates (one shot, indexed by symbol)
    funding_map = fetch_kraken_funding_rates()

    # 3) For each non-stable coin, fetch RSI input series
    excluded_in_universe = CRYPTO_EXCLUDE & {c["symbol"].upper() for c in markets}
    print(f"Scanning top 50 crypto ({len(excluded_in_universe)} stablecoins/wrapped excluded)...",
          file=sys.stderr)

    results: list[dict] = []
    for coin in markets:
        sym = coin["symbol"].upper()
        if sym in CRYPTO_EXCLUDE:
            continue

        # Daily closes for last 30 days -> enough for RSI(14) + 7d vol avg
        payload = _coingecko_get(
            f"https://api.coingecko.com/api/v3/coins/{coin['id']}/market_chart",
            {"vs_currency": "usd", "days": "30", "interval": "daily"},
        )
        if not payload or not isinstance(payload, dict):
            time.sleep(COINGECKO_DELAY)
            continue

        try:
            prices = payload.get("prices", [])
            volumes = payload.get("total_volumes", [])
            if len(prices) < 20 or len(volumes) < 8:
                time.sleep(COINGECKO_DELAY)
                continue

            closes = pd.Series([p[1] for p in prices])
            vols   = pd.Series([v[1] for v in volumes])

            rsi_14 = float(rsi(closes).iloc[-1])
            # 24h vol is the last entry; 7d average is mean of the previous 7.
            vol_24h = float(vols.iloc[-1])
            avg_vol_7d = float(vols.iloc[-8:-1].mean())
            vol_surge = vol_24h / avg_vol_7d if avg_vol_7d > 0 else 0.0
            change_24h = float(coin.get("price_change_percentage_24h") or 0)

            # Filter conditions
            rsi_ok   = CRYPTO_RSI_MIN <= rsi_14 <= CRYPTO_RSI_MAX
            vol_ok   = vol_surge >= CRYPTO_VOL_SURGE_MIN
            price_up = change_24h > 0

            if not (rsi_ok and vol_ok and price_up):
                time.sleep(COINGECKO_DELAY)
                continue

            funding_pct = funding_map.get(sym)
            funding_flag = ""
            if funding_pct is not None:
                annualized = funding_pct * 24 * 365
                if annualized > 20:
                    funding_flag = " | ⚠ crowded longs (high funding)"
                elif annualized < -20:
                    funding_flag = " | shorts paying (contrarian long setup)"

            score = vol_surge * (rsi_14 - 50)

            results.append({
                "Ticker": sym,
                "Price": round(coin["current_price"], 6 if coin["current_price"] < 1 else 2),
                "Vol Change": round(vol_surge, 2),
                "RSI": round(rsi_14, 1),
                "24h %": round(change_24h, 2),
                "Funding %": round(funding_pct, 4) if funding_pct is not None else None,
                "Score": round(score, 2),
                "Bullish Reason": (
                    f"+{change_24h:.1f}% on {vol_surge:.1f}x 7d-avg volume with "
                    f"RSI at {rsi_14:.0f} (momentum zone){funding_flag}."
                ),
            })
        except Exception:
            pass
        time.sleep(COINGECKO_DELAY)

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results).sort_values("Score", ascending=False).head(top_n)
    return out.reset_index(drop=True)


# -----------------------------------------------------------------------------
# Output formatting
# -----------------------------------------------------------------------------

def print_table(title: str, df: pd.DataFrame) -> None:
    bar = "=" * 100
    print(f"\n{bar}\n{title}\n{bar}")
    if df.empty:
        print("  No results matched the filter criteria.")
        return
    # Show all columns except Score (used for ranking, implied by row order)
    display_df = df.drop(columns=["Score"], errors="ignore")
    print(display_df.to_string(index=False))


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Daily stock + crypto momentum scanner.")
    parser.add_argument("--top", type=int, default=10, help="Top N results per asset class (default 10)")
    parser.add_argument("--stocks-only", action="store_true")
    parser.add_argument("--crypto-only", action="store_true")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Save combined results to a CSV")
    args = parser.parse_args()

    stock_df = pd.DataFrame()
    crypto_df = pd.DataFrame()

    if not args.crypto_only:
        stock_df = scan_stocks(top_n=args.top)
        print_table(f"TOP {args.top} STOCKS (S&P 500 + Nasdaq 100)", stock_df)

    if not args.stocks_only:
        crypto_df = scan_crypto(top_n=args.top)
        print_table(f"TOP {args.top} CRYPTO (Top 50 by market cap)", crypto_df)

    if args.output:
        frames = []
        if not stock_df.empty:
            s = stock_df.copy()
            s.insert(0, "AssetClass", "Stock")
            frames.append(s)
        if not crypto_df.empty:
            c = crypto_df.copy()
            c.insert(0, "AssetClass", "Crypto")
            frames.append(c)
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(args.output, index=False)
            print(f"\nSaved to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
