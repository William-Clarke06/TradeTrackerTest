import logging
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# yfinance logs "Data doesn't exist..." whenever we ask for a session that hasn't
# happened yet — expected for after-hours filings that defer to the next open.
logging.getLogger('yfinance').setLevel(logging.CRITICAL)


def _safe_history(ticker_or_tk, **kwargs):
    """
    Fetch price history without ever raising.

    yfinance leaks raw exceptions (KeyError: 'tradingPeriods',
    'currentTradingPeriod', 'exchangeTimezoneName', rate-limit errors, ...)
    whenever Yahoo hands back partial or empty metadata — delisted tickers,
    instruments with no intraday data, throttling, or a transient hiccup. One
    such failure inside a single ticker used to abort the whole run. Here we
    swallow it and return an EMPTY DataFrame, so every caller hits its existing
    `.empty` path: defer, leave the price NULL, retry next cycle — exactly how a
    legitimately-missing session is already handled.

    Accepts either a ticker string or an existing yf.Ticker (so callers that
    reuse a Ticker keep its cached cookie/crumb).
    """
    tk = ticker_or_tk if isinstance(ticker_or_tk, yf.Ticker) else yf.Ticker(ticker_or_tk)
    kwargs.setdefault('auto_adjust', True)   # <-- add: deterministic split-adjusted prices
    try:
        df = tk.history(**kwargs)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "history failed for %s (%s: %s); treating as no data",
            getattr(tk, 'ticker', ticker_or_tk), type(e).__name__, e,
        )
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()

def cumulative_split_factor(tk, since_date):
    """
    Product of split ratios that took effect AFTER `since_date`.

    With auto_adjust=True a fresh history is in CURRENT (post-split) share units,
    while the frozen entry_price is in entry-era units. Multiplying a current-units
    price by this factor converts it back to entry-era units, so entry vs peak/latest
    are compared in one regime instead of mixing pre- and post-split scales.
    Returns 1.0 when there are no later splits (the common case) or on lookup failure.
    """
    try:
        splits = tk.splits
    except Exception:
        return 1.0
    if splits is None or len(splits) == 0:
        return 1.0
    factor = 1.0
    for ts, ratio in splits.items():
        if ts.date() > since_date and ratio:
            factor *= float(ratio)
    return factor


def classify_trade_type(trade_type):
    """
    Map an openinsider trade type to a trade direction, or None to drop the row.
    Only the two clean directional types are kept:
        'P - Purchase' -> 'long'
        'S - Sale'     -> 'short'
    Everything else (S - Sale+OE, F - Tax, and A/D/E/M-flagged rows) is
    mechanical/noise and returns None so it never enters the DB.
    """
    t = trade_type.strip()
    if t == 'P - Purchase':
        return 'long'
    if t == 'S - Sale':
        return 'short'
    return None


def get_entry_price(ticker, filing_datetime):
    """
    Find the first tradeable price at or after the filing timestamp (FPTP).
    filing_datetime is a string like '2026-08-19 21:56:33'.
    Tries minute data first; falls back to next available daily open.
    Returns (price, method, entry_time), or (None, None, None).

    Direction-independent: you enter a long or a short at the same first
    tradeable price; only the peak/return math differs afterward.
    """
    filing_dt = datetime.strptime(filing_datetime[:19], '%Y-%m-%d %H:%M:%S')

    # --- Try minute data (only works within ~30 days of now) ---
    start = filing_dt.date()
    end = start + timedelta(days=2)
    minute = _safe_history(
        ticker,
        start=start.strftime('%Y-%m-%d'),
        end=end.strftime('%Y-%m-%d'),
        interval='1m',
    )

    if not minute.empty:
        filing_ts = pd.Timestamp(filing_dt, tz=minute.index.tz)
        after = minute[minute.index >= filing_ts]
        if not after.empty:
            entry_time = after.index[0].strftime('%Y-%m-%d %H:%M')
            return float(after['Open'].iloc[0]), 'minute', entry_time

    # --- Fallback: first daily session whose OPEN is at/after the filing ---
    # Don't trust `start` — on weekends yfinance hands back the prior close, which
    # would be look-ahead. Instead check each returned bar: accept the first
    # session whose 09:30 open is at/after the filing moment. If none qualify yet
    # (e.g. after-hours filing, next session hasn't happened), return None and let
    # a later run fill it.
    daily = _safe_history(
        ticker,
        start=start.strftime('%Y-%m-%d'),
        end=(start + timedelta(days=9)).strftime('%Y-%m-%d'),
        interval='1d',
    )
    if not daily.empty:
        filing_ts = pd.Timestamp(filing_dt, tz=daily.index.tz)
        for ts in daily.index:
            session_open = ts.replace(hour=9, minute=30)
            if session_open >= filing_ts:
                return float(daily.loc[ts, 'Open']), 'daily_fallback', ts.strftime('%Y-%m-%d')

    return None, None, None


def _entry_day_extreme(tk, entry_dt, entry_date, is_long):
    """
    The most favorable price at or after the entry timestamp on the entry day
    itself, from 1-minute bars: the High (long) or Low (short) of minutes at or
    after entry. Returns (price, 'YYYY-MM-DD HH:MM') or (None, None) when Yahoo
    no longer serves minute data for that day (~30 days old).
    """
    m = _safe_history(
        tk,
        start=entry_date.strftime('%Y-%m-%d'),
        end=(entry_date + timedelta(days=1)).strftime('%Y-%m-%d'),
        interval='1m',
    )
    if m.empty:
        return None, None
    entry_ts = pd.Timestamp(entry_dt, tz=m.index.tz)
    after = m[m.index >= entry_ts]
    if after.empty:
        return None, None
    col = 'High' if is_long else 'Low'
    idx = after[col].idxmax() if is_long else after[col].idxmin()
    return float(after.loc[idx, col]), idx.strftime('%Y-%m-%d %H:%M')


def get_price_stats(ticker, entry_datetime, entry_method, direction):
    """
    Track how the stock moved from the ENTRY (FPTP) moment forward, in the
    direction of the trade.

    'peak' here means the most FAVORABLE price reached since entry:
        long  -> highest High
        short -> lowest  Low
    measured strictly at/after entry so we never credit a move that happened
    before we could have entered. Entry day handled per method:
      - 'daily_fallback': entered at the open, so the whole entry-day extreme is
        capturable.
      - 'minute': entered intraday, so on the entry day we count only minutes
        at/after the entry timestamp. Yahoo keeps 1-minute data ~30 days; once
        it's gone we conservatively DROP the entry day rather than fall back to
        the whole-day extreme (which would re-introduce pre-entry look-ahead).
        Pair this with a running-best `peak` in storage so the precise value
        captured while the trade was young is retained.

    direction is 'long' or 'short'. Returns a dict, or None if there's no data.
    """
    is_long = (direction == 'long')
    col = 'High' if is_long else 'Low'

    def extreme(series):
        return series.max() if is_long else series.min()

    def extreme_idx(series):
        return series.idxmax() if is_long else series.idxmin()

    def beats(candidate, best):
        if best is None:
            return True
        return candidate > best if is_long else candidate < best

    tk = yf.Ticker(ticker)

    # Parse the entry moment, tolerating both timestamp and date-only forms.
    if len(entry_datetime) > 10:
        entry_dt = datetime.strptime(entry_datetime[:16], '%Y-%m-%d %H:%M')
    else:
        entry_dt = datetime.strptime(entry_datetime[:10], '%Y-%m-%d')
    entry_date = entry_dt.date()

    daily = _safe_history(tk, start=entry_date.strftime('%Y-%m-%d'), interval='1d')
    if daily.empty:
        return None

    latest_price = float(daily['Close'].iloc[-1])
    latest_date = daily.index[-1].strftime('%Y-%m-%d')

    peak_price = None
    peak_date = None

    # Full days AFTER the entry day are always fair game.
    later = daily[daily.index.date > entry_date]
    if not later.empty:
        peak_price = float(extreme(later[col]))
        peak_date = extreme_idx(later[col]).strftime('%Y-%m-%d')

    # The entry day itself, handled per entry method.
    entry_day = daily[daily.index.date == entry_date]
    if not entry_day.empty:
        if entry_method == 'daily_fallback':
            cand = float(entry_day[col].iloc[0])
            cand_when = entry_date.strftime('%Y-%m-%d')
        else:
            cand, cand_when = _entry_day_extreme(tk, entry_dt, entry_date, is_long)
        if cand is not None and beats(cand, peak_price):
            peak_price, peak_date = cand, cand_when

    # Freshen 'latest' with an intraday tick; let today's live extreme (a
    # post-entry day) improve the peak. Gated on today > entry_date so we never
    # count a pre-entry tick on the entry day.
    intraday = _safe_history(tk, period='1d', interval='1m')
    if not intraday.empty:
        latest_price = float(intraday['Close'].iloc[-1])
        latest_date = intraday.index[-1].strftime('%Y-%m-%d %H:%M')
        if intraday.index[-1].date() > entry_date:
            cand = float(extreme(intraday[col]))
            if beats(cand, peak_price):
                peak_price = cand
                peak_date = extreme_idx(intraday[col]).strftime('%Y-%m-%d %H:%M')

    # Only reachable if the entry day is the sole day AND its minutes are gone.
    if peak_price is None:
        peak_price, peak_date = latest_price, latest_date

    # Re-express fresh (current-units) values into entry-era units so they line
    # up with the frozen entry_price across any split that occurred after entry.
    factor = cumulative_split_factor(tk, entry_date)
    if factor != 1.0:
        peak_price *= factor
        latest_price *= factor

    return {
        'latest_price': latest_price,
        'latest_date':  latest_date,
        'peak_price':   peak_price,
        'peak_date':    peak_date,
        'split_factor': factor,          # <-- add this line
    }


if __name__ == '__main__':
    # LONG: AVBC purchase, mid-day entry (~09:45) -> entry-day peak should count
    # only minutes at/after entry.
    p, m, t = get_entry_price('AVBC', '2026-08-19 09:44:52')
    print('LONG  AVBC', p, m, t)
    print('     ', get_price_stats('AVBC', t, m, 'long'))

    # SHORT: APPF sale -> 'peak' should track the LOWEST low since entry.
    p, m, t = get_entry_price('APPF', '2026-08-20 18:35:19')
    print('SHORT APPF', p, m, t)
    print('     ', get_price_stats('APPF', t, m, 'short'))
