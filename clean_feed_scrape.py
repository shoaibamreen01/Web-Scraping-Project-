#!/usr/bin/env python3
"""
feed_price_multi_source.py

Improved multi-source poultry-feed scraper:
- Scrapes live pages and reconstructs historical tables via Wayback (CDX).
- Better price/date detection, removes time portion (YYYY-MM-DD).
- Normalizes PKR numeric values to price_50kg_rs when possible.
- Saves deduped CSV: data/feed_price_multi_source.csv
"""
from __future__ import annotations
import os
import time
import json
import re
import math
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from tqdm import tqdm

# ---------------- CONFIG ----------------
OUTPUT_DIR = "data"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "feed_price_multi_source.csv")

TARGET_PAGES = {
    "zeeshanagro": "https://zeeshanagro.com/poultry-feed-price-in-pakistan/",
    "pricesin": "https://pricesin.pk/poultry-feed-prices-in-pakistan/",
    "priceindex": "https://priceindex.pk/chicken-poultry-feed-price-pakistan/",
}

WAYBACK_FROM = datetime.utcnow() - timedelta(days=365 * 6)
WAYBACK_TO = datetime.utcnow()
WAYBACK_LIMIT = 200  # snapshots to fetch per page (kept reasonable)
REQUEST_DELAY = 0.5  # polite delay between requests

# acceptable price ranges for 50kg bag (heuristic)
MIN_50KG = 1000
MAX_50KG = 20000

# requests session with retries
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (FeedHistoryBot/1.0)"})
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount("http://", adapter)
session.mount("https://", adapter)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


# ---------------- HELPERS ----------------
def download_text(url: str, timeout: int = 30) -> Optional[str]:
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.debug(f"download_text failed for {url}: {e}")
        return None


def parse_tables_from_html(html_text: str) -> List[pd.DataFrame]:
    if not html_text:
        return []
    soup = BeautifulSoup(html_text, "lxml")
    tables = soup.find_all("table")
    dfs = []
    for t in tables:
        try:
            df = pd.read_html(str(t))[0]
            if df.shape[0] > 0 and df.shape[1] > 0:
                dfs.append(df)
        except Exception:
            continue
    return dfs


def clean_price_token(token: Any) -> Optional[float]:
    """
    Extract numeric price from a cell. Returns numeric value or None.
    """
    if token is None:
        return None
    s = str(token)
    # remove common noise
    s = re.sub(r'[\u200b,\xa0]', '', s)  # zero-width and NBSP and commas
    s = re.sub(r'(PKR|pkr|Rs\.?|rs\.?)', '', s, flags=re.IGNORECASE)
    s = s.strip()
    # find numbers with optional decimals
    m = re.search(r'(-?\d{1,6}(?:\.\d+)?)', s)
    if not m:
        return None
    try:
        val = float(m.group(1))
        if math.isfinite(val):
            # if value is negative or zero -> ignore
            if val <= 0:
                return None
            return float(val)
    except Exception:
        return None
    return None


def detect_price_unit(text: str) -> str:
    """
    Heuristic to detect unit mentions in text.
    return: "50kg", "kg", "bag", or ""
    """
    txt = (text or "").lower()
    if "50kg" in txt or "50 kg" in txt or "50-kg" in txt or "50 kg bag" in txt:
        return "50kg"
    if "kg" in txt and ("per" in txt or "/kg" in txt or "kg)" in txt):
        return "kg"
    if "bag" in txt:
        return "bag"
    return ""


def to_iso_date_only(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.date().isoformat()


def try_parse_date(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    s = str(val).strip()
    # remove times if present
    # common patterns: '2025-03-16 12:34', '16 Mar 2025 14:00'
    if not s:
        return None
    # If the string is numeric timestamp (e.g., 20250316...), try common patterns
    try:
        # dateutil can parse many forms
        dt = dateparser.parse(s, fuzzy=True, dayfirst=False)
        if dt:
            return dt
    except Exception:
        pass
    return None


def extract_date_from_df_row_values(row: pd.Series) -> Optional[datetime]:
    """
    Inspect all cells in the row for date-like strings.
    """
    for v in row.astype(str).values:
        dt = try_parse_date(v)
        if dt:
            return dt
    return None


def normalize_dataframe(df: pd.DataFrame, source_name: str, snapshot_dt: Optional[datetime] = None,
                        page_html: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Attempt to identify feed (name), price columns, and date column for a table, then emit normalized rows.
    """
    rows_out: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return rows_out

    # normalize column labels to strings
    orig_cols = [str(c) for c in df.columns]
    cols_lower = [c.lower() for c in orig_cols]

    # Determine name column heuristically
    name_candidates = []
    for i, c in enumerate(cols_lower):
        if any(k in c for k in ("feed", "poultry", "type", "product", "ration", "name")):
            name_candidates.append(orig_cols[i])
    if not name_candidates:
        # pick the textual column (highest proportion of alphabetic content)
        text_scores = {}
        for c in orig_cols:
            col_as_str = df[c].astype(str)
            alpha_frac = (col_as_str.str.replace(r'[^A-Za-z]', '', regex=True).str.len() /
                          col_as_str.str.len().replace(0, 1)).fillna(0).mean()
            text_scores[c] = alpha_frac
        # pick the column with the highest text fraction
        name_candidates = [max(text_scores, key=text_scores.get)]

    name_col = name_candidates[0]

    # Determine price-like columns
    price_candidates = []
    for i, c in enumerate(cols_lower):
        if any(k in c for k in ("price", "rate", "pkr", "rs", "bag", "50kg", "50 kg", "/kg", "kg")):
            price_candidates.append(orig_cols[i])

    if not price_candidates:
        # fallback: look for columns containing numeric patterns in first N rows
        for c in orig_cols:
            sample = " ".join(df[c].astype(str).head(10).tolist())
            if re.search(r'\d{3,6}', sample):
                price_candidates.append(c)

    # Exclude name_col from price candidates if present
    price_candidates = [c for c in price_candidates if c != name_col]

    # Date column detection (explicit)
    date_col = None
    for i, c in enumerate(cols_lower):
        if "date" in c or "updated" in c or "as on" in c or "on" == c.strip():
            date_col = orig_cols[i]
            break

    # If page_html provided, attempt to find page-level update date
    page_level_date = None
    if page_html:
        # try patterns like "Updated: 16 March 2025" or "Last updated: 16/03/2025"
        m = re.search(r'(Last\s+updated|Updated|Updated on|As on|On)\s*[:\-]?\s*([A-Za-z0-9,\/\-\s]+)',
                      page_html, flags=re.IGNORECASE)
        if m:
            dt = try_parse_date(m.group(2))
            if dt:
                page_level_date = dt

    # For each row, build normalized record(s)
    for idx, row in df.iterrows():
        try:
            feed_name_raw = row.get(name_col, "")
        except Exception:
            feed_name_raw = ""
        feed_name = str(feed_name_raw).strip()

        # If the feed name looks numeric or empty, try to find a better candidate in the row
        if not feed_name or re.fullmatch(r'^\d+(\.\d+)?$', feed_name):
            # pick the first column with alphabetic content of reasonable length
            for c in orig_cols:
                cell = str(row.get(c, "")).strip()
                if len(cell) > 2 and re.search(r'[A-Za-z]', cell):
                    feed_name = cell
                    break

        # Determine row date precedence:
        # 1) explicit date column, 2) snapshot_dt (Wayback), 3) page_level_date, 4) try to extract date from row cells
        row_date = None
        if date_col:
            row_date = try_parse_date(row.get(date_col))
        if not row_date and snapshot_dt:
            row_date = snapshot_dt
        if not row_date and page_level_date:
            row_date = page_level_date
        if not row_date:
            row_date = extract_date_from_df_row_values(row)

        # If still None, leave as None (will be blank in output)
        # Determine price entries in the row
        found_price = False
        # look through candidate price columns
        for pc in price_candidates:
            raw = row.get(pc, "")
            price_val = clean_price_token(raw)
            if price_val:
                found_price = True
                unit = detect_price_unit(str(pc) + " " + str(raw))
                # Heuristic: if unit indicates per kg, multiply by 50
                if unit == "kg":
                    price_50kg = round(price_val * 50)
                elif unit == "50kg" or unit == "bag":
                    price_50kg = round(price_val)
                else:
                    # unknown: try to guess: if price_val <= MAX_50KG and >= MIN_50KG -> assume it's 50kg
                    if MIN_50KG <= price_val <= MAX_50KG:
                        price_50kg = round(price_val)
                    elif 10 <= price_val <= 500:  # likely per kg
                        price_50kg = round(price_val * 50)
                    else:
                        price_50kg = round(price_val)
                rows_out.append({
                    "date": to_iso_date_only(row_date),
                    "source": source_name,
                    "feed_name": feed_name,
                    "price_column": pc,
                    "price_50kg_rs": price_50kg,
                    "raw_price_text": str(raw),
                    "raw_row": json.dumps({c: str(row.get(c, "")) for c in orig_cols})
                })

        # No price found in candidate columns: try scanning whole row for numbers
        if not found_price:
            # scan every cell for numbers
            for c in orig_cols:
                raw = row.get(c, "")
                price_val = clean_price_token(raw)
                if price_val:
                    unit = detect_price_unit(str(c) + " " + str(raw))
                    if unit == "kg":
                        price_50kg = round(price_val * 50)
                    elif unit == "50kg" or unit == "bag":
                        price_50kg = round(price_val)
                    else:
                        if MIN_50KG <= price_val <= MAX_50KG:
                            price_50kg = round(price_val)
                        elif 10 <= price_val <= 500:
                            price_50kg = round(price_val * 50)
                        else:
                            price_50kg = round(price_val)
                    rows_out.append({
                        "date": to_iso_date_only(row_date),
                        "source": source_name,
                        "feed_name": feed_name,
                        "price_column": c,
                        "price_50kg_rs": price_50kg,
                        "raw_price_text": str(raw),
                        "raw_row": json.dumps({c2: str(row.get(c2, "")) for c2 in orig_cols})
                    })
                    found_price = True
                    break

        # If still no price, add a row with empty price (keeps feed_name and raw row for context)
        if not found_price:
            rows_out.append({
                "date": to_iso_date_only(row_date),
                "source": source_name,
                "feed_name": feed_name,
                "price_column": "",
                "price_50kg_rs": None,
                "raw_price_text": "",
                "raw_row": json.dumps({c: str(row.get(c, "")) for c in orig_cols})
            })

    return rows_out


# ---------------- WAYBACK / CDX ----------------
def get_snapshots(url: str, from_dt: datetime, to_dt: datetime, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Query Wayback CDX API for snapshots (timestamps). Returns list of dicts {"timestamp":..., "dt":...}
    """
    api = (
        "http://web.archive.org/cdx/search/cdx?url={url}&output=json&from={frm}&to={to}&fl=timestamp,original&collapse=digest"
        "&filter=statuscode:200"
    ).format(url=url, frm=from_dt.strftime("%Y%m%d"), to=to_dt.strftime("%Y%m%d"))
    try:
        r = session.get(api, timeout=30)
        r.raise_for_status()
        data = r.json()
        snapshots = []
        # first item is header
        for row in data[1:limit+1]:
            ts = row[0]
            try:
                dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
            except Exception:
                # fallback: parse partial timestamp
                dt = try_parse_date(ts)
            snapshots.append({"timestamp": ts, "dt": dt})
        return snapshots
    except Exception as e:
        logging.debug(f"get_snapshots failed for {url}: {e}")
        return []


def fetch_html(url: str, timeout: int = 25) -> Optional[str]:
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.debug(f"fetch_html failed for {url}: {e}")
        return None


def reconstruct_wayback_for_page(name: str, url: str, from_dt: datetime, to_dt: datetime, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Fetch wayback snapshots and extract normalized rows from each snapshot table.
    """
    all_rows: List[Dict[str, Any]] = []
    snapshots = get_snapshots(url, from_dt, to_dt, limit=limit)
    if not snapshots:
        logging.info(f"[WAYBACK] No snapshots found for {name}")
        return all_rows

    logging.info(f"[WAYBACK] {name}: {len(snapshots)} snapshots (using up to {limit})")
    for snap in tqdm(snapshots, desc=f"Wayback {name}", unit="snap"):
        ts = snap["timestamp"]
        snap_url = f"https://web.archive.org/web/{ts}/{url}"
        html = fetch_html(snap_url)
        if not html:
            time.sleep(REQUEST_DELAY)
            continue

        # parse tables and normalize
        try:
            dfs = parse_tables_from_html(html)
            for df in dfs:
                rows = normalize_dataframe(df, source_name=f"{name}_wayback", snapshot_dt=snap["dt"], page_html=html)
                all_rows.extend(rows)
        except Exception as e:
            logging.debug(f"Error parsing snapshot {ts} for {name}: {e}")

        time.sleep(REQUEST_DELAY)
    return all_rows


# ---------------- LIVE SCRAPER ----------------
def scrape_live_pages(pages: Dict[str, str]) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    for name, url in pages.items():
        logging.info(f"[LIVE] Fetching: {name} -> {url}")
        html = download_text(url)
        if not html:
            logging.warning(f"[LIVE] Failed to download {url}")
            continue

        tables = parse_tables_from_html(html)
        if not tables:
            # Sometimes tables are not marked up <table>; attempt to parse HTML lists or preformatted blocks? (not implemented)
            logging.debug(f"[LIVE] No <table> found on {url}")
        for df in tables:
            try:
                rows = normalize_dataframe(df, source_name=name, snapshot_dt=datetime.utcnow(), page_html=html)
                all_rows.extend(rows)
            except Exception as e:
                logging.debug(f"[LIVE] normalize_dataframe failed for {name}: {e}")

        time.sleep(REQUEST_DELAY)
    return all_rows


# ---------------- SAVE/Dedupe ----------------
def save_and_dedupe(rows: List[Dict[str, Any]], out_file: str) -> Optional[pd.DataFrame]:
    if not rows:
        logging.info("No rows extracted.")
        return None

    df = pd.DataFrame(rows)
    # convert empty-string dates to NaT, and parse iso date strings (we only store date)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date

    # Validate price_50kg_rs numeric and sensible range; keep None for others
    def valid_price(x):
        if pd.isna(x):
            return False
        try:
            xv = float(x)
            return MIN_50KG <= xv <= MAX_50KG
        except Exception:
            return False

    # Keep rows where price is valid OR price is null (we want feed metadata too)
    df_valid_prices = df[df["price_50kg_rs"].apply(lambda x: valid_price(x) or pd.isna(x))].copy()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if os.path.exists(out_file):
        try:
            old = pd.read_csv(out_file, parse_dates=["date"])
            # ensure date column is date only, not datetime
            old["date"] = pd.to_datetime(old["date"], errors="coerce").dt.date
            combined = pd.concat([old, df_valid_prices], ignore_index=True, sort=False)
        except Exception:
            combined = df_valid_prices
    else:
        combined = df_valid_prices

    # dedupe by date (day), source, feed_name, price
    combined["date_day"] = pd.to_datetime(combined["date"], errors="coerce").dt.date
    # if date_day is NaT, keep them but dedupe separately - use fillna with a unique placeholder
    combined["date_day_fill"] = combined["date_day"].astype(str).fillna("NO_DATE")
    combined_before = len(combined)
    combined.drop_duplicates(subset=["date_day_fill", "source", "feed_name", "price_50kg_rs"], inplace=True)
    combined_after = len(combined)

    # cleanup helper columns
    combined.drop(columns=["date_day", "date_day_fill"], inplace=True, errors="ignore")

    # sort and write
    # convert date column back to ISO strings (YYYY-MM-DD) for CSV stability
    combined["date"] = combined["date"].apply(lambda d: d.isoformat() if (pd.notna(d) and hasattr(d, "isoformat")) else (str(d) if pd.notna(d) else ""))

    combined.sort_values(by=["date", "source", "feed_name"], inplace=True, na_position="last")
    combined.to_csv(out_file, index=False)
    logging.info(f"Saved {len(combined)} rows to {out_file} (added {combined_after - (0 if os.path.exists(out_file) else 0)} unique rows)")
    return combined


# ---------------- MAIN ----------------
def main():
    logging.info("=== Multi-source feed price scraper (improved) ===")
    wayback_rows_all: List[Dict[str, Any]] = []

    # Wayback reconstruction
    for name, url in TARGET_PAGES.items():
        logging.info(f"[MAIN] Wayback reconstruct for: {name}")
        rows = reconstruct_wayback_for_page(name, url, WAYBACK_FROM, WAYBACK_TO, limit=WAYBACK_LIMIT)
        wayback_rows_all.extend(rows)

    logging.info(f"Wayback total rows collected: {len(wayback_rows_all)}")

    # Live scrape
    live_rows = scrape_live_pages(TARGET_PAGES)
    logging.info(f"Live scrape rows: {len(live_rows)}")

    all_rows = wayback_rows_all + live_rows
    combined = save_and_dedupe(all_rows, OUTPUT_FILE)

    if combined is not None:
        logging.info("\nLatest sample:")
        with pd.option_context('display.max_rows', 20, 'display.max_columns', None):
            print(combined.tail(20).to_string(index=False))


if __name__ == "__main__":
    main()
