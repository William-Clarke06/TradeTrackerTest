import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

def get_entry_price(ticker, filing_datetime):
    """Find first tradable price after filing timestamp.
       trys minute data; falls back to next open.
       returns (price, method) so we know which was used."""

    filing_dt = datetime.strptime(filing_datetime[:19], '%Y-%m-%d %H:%M:%S') # Cleans into real dt.

    # Makes minutes table from filing date + next day (wont work sat & sun).
    start = filing_dt.date()
    end = start + timedelta(days=2)
    minute = yf.Ticker(ticker).history( # collects data
        start=start.strftime('%Y-%m-%d'),
        end=end.strftime('%Y-%m-%d'),
        interval='1m',
    )

    if not minute.empty: 
        #makes filing time comparable, then takes first bar after it.
        filing_ts = pd.Timestamp(filing_dt, tz=minute.index.tz) # Adds timestamp to filing.
        after = minute[minute.index >= filing_ts] # Removes all minute data before filing.
        if not after.empty:
            return float(after['Open'].iloc[0]), 'minute' # Takes 'Open' value from first (iloc[0]) row remaining. 


    # Fallback, next daily open
    daily = yf.Ticker(ticker).history(
        start=start.strftime('%Y-%m-%d'),
        end=(start + timedelta(days=7)).strftime('%Y-%m-%d'),
        interval='1d',
    )
    if not daily.empty:
        return float(daily['Open'].iloc[0]), 'daily_fallback'

    return None, None


if __name__ == '__main__':
    print(get_entry_price('AVBC', '2026-08-19 09:44:52'))
