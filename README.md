# Web Scraping Project - Poultry Feed Price Tracker

A Python-based web scraper that collects poultry feed prices from multiple sources in Pakistan.

## Features

- **Multi-source scraping**: Scrapes live pricing data from zeeshanagro, pricesin, and priceindex
- **Historical data recovery**: Uses Wayback Machine (CDX API) to reconstruct historical price tables
- **Data normalization**: Converts prices to standardized 50kg bag pricing in PKR
- **Automated collection**: Fetches and deduplicates data with proper date handling

## Files

- `clean_feed_scrape.py` - Main scraper script with multi-source data collection
- `feed_price_multi_source.csv` - Collected feed price data with timestamps and sources

## Requirements

- Python 3.7+
- requests
- pandas
- beautifulsoup4
- python-dateutil
- tqdm

## Installation

```bash
pip install requests pandas beautifulsoup4 python-dateutil tqdm
```

## Usage

```bash
python clean_feed_scrape.py
```

The script will scrape current prices and historical data, then save results to `feed_price_multi_source.csv`.

## Data Structure

The output CSV contains:
- `date` - Collection date (YYYY-MM-DD format)
- `source` - Data source identifier
- `feed_name` - Type of poultry feed
- `price_50kg_rs` - Normalized price for 50kg bag in PKR
- `raw_price_text` - Original price text from source
- `raw_row` - Complete raw data row

## License

MIT
