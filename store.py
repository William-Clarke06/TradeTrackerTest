import sqlite3
from datetime import date, datetime
from prices import get_entry_price, get_price_stats, classify_trade_type

DB_PATH = 'trades.db'
TEST_WINDOW_DAYS = 60          # a trade is tracked for this many days from FPTP


# ----------------------------------------------------------------------------
# connection + schema
# ----------------------------------------------------------------------------
def connect():
    return sqlite3.connect(DB_PATH)


def create_table():
    """
    Fresh-DB schema. There is no migrate() any more: start the 60-day run on a
    clean file. If an OLD trades.db is present, delete/rename it first, or this
    CREATE ... IF NOT EXISTS is a no-op and inserts will fail on the old columns.
    """
    conn = connect()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_group    TEXT,
            ticker          TEXT,
            trade_type      TEXT,
            direction       TEXT,          -- 'long' (P) or 'short' (S)
            trade_date      TEXT,
            filing_datetime TEXT,
            insider_price   REAL,
            insider_qty     INTEGER,
            insider_value   REAL,
            first_seen      TEXT,
            status          TEXT DEFAULT 'active',
            entry_price     REAL,          -- FPTP
            entry_datetime  TEXT,
            entry_method    TEXT,
            peak_price      REAL,          -- most FAVOURABLE price since entry
            peak_datetime   TEXT,
            latest_price    REAL,
            latest_datetime TEXT,
            UNIQUE(ticker, trade_date, trade_type, insider_qty, source_group)
        )
    ''')
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# small pure helpers (used on read; no DB, no network)
# ----------------------------------------------------------------------------
def clean_number(text):
    """'$1,234.50' -> 1234.5 ; '-8,056' -> -8056.0 ; '' -> None. Keeps sign."""
    cleaned = text.replace('$', '').replace(',', '').replace('+', '').strip()
    if cleaned == '':
        return None
    return float(cleaned)


def pct_move(entry, price, direction):
    """Return %, signed for the trade's direction. Short profits when price falls."""
    if entry is None or price is None or entry == 0:
        return None
    raw = (price - entry) / entry * 100.0
    return raw if direction == 'long' else -raw


def size_tier(entry_price):
    """Bucket a trade by its buy-in (FPTP) price. Half-open bands, no overlap."""
    if entry_price is None:
        return 'unknown'
    p = entry_price
    if p < 1:   return '<$1'
    if p < 5:   return '$1-4.99'
    if p < 15:  return '$5-14.99'
    if p < 50:  return '$15-49.99'
    return '$50+'


def days_between(entry_datetime, other_datetime):
    """Whole days from entry to another timestamp (date-level)."""
    if not entry_datetime or not other_datetime:
        return None
    e = datetime.strptime(entry_datetime[:10], '%Y-%m-%d').date()
    o = datetime.strptime(other_datetime[:10], '%Y-%m-%d').date()
    return (o - e).days


# ----------------------------------------------------------------------------
# ingest
# ----------------------------------------------------------------------------
def save_trades(trades):
    """
    Insert new trades. Non-directional types (S - Sale+OE, F - Tax, A/D/E/M) are
    dropped here via classify_trade_type, so they never enter the DB. Identity is
    (ticker, trade_date, trade_type, insider_qty, source_group); dupes are ignored.
    """
    conn = connect()
    cursor = conn.cursor()
    today = date.today().isoformat()

    inserted = 0
    skipped = 0
    for t in trades:
        direction = classify_trade_type(t['trade_type'])
        if direction is None:            # not a clean P/S trade -> drop it
            skipped += 1
            continue
        cursor.execute('''
            INSERT OR IGNORE INTO trades
                (source_group, ticker, trade_type, direction, trade_date,
                 filing_datetime, insider_price, insider_qty, insider_value,
                 first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            t['group'],
            t['ticker'],
            t['trade_type'],
            direction,
            t['trade_date'],
            t['filing_date'],                 # collector still emits 'filing_date'
            clean_number(t['price']),
            clean_number(t['qty']),
            clean_number(t['value']),
            today,
        ))
        inserted += cursor.rowcount

    conn.commit()
    conn.close()
    print(f"Saved {inserted} new ({len(trades)} seen, {skipped} non-P/S skipped).")


# ----------------------------------------------------------------------------
# FPTP (entry) — a fixed past fact, fetched once
# ----------------------------------------------------------------------------
def fill_entry_prices():
    """Look up FPTP for any trade that doesn't have one yet (any status)."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, ticker, filing_datetime FROM trades WHERE entry_price IS NULL")
    rows = cursor.fetchall()

    for trade_id, ticker, filing_dt in rows:
        price, method, entry_dt = get_entry_price(ticker, filing_dt)
        cursor.execute(
            "UPDATE trades SET entry_price = ?, entry_method = ?, entry_datetime = ? WHERE id = ?",
            (price, method, entry_dt, trade_id),
        )
        print(f"  entry {ticker}: {price} ({method}) @ {entry_dt}")

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# 60-day age-out — frozen trades stop refreshing but stay for history/averages
# ----------------------------------------------------------------------------
def age_out(max_days=TEST_WINDOW_DAYS):
    conn = connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, entry_datetime FROM trades WHERE status = 'active' AND entry_datetime IS NOT NULL"
    )
    today = date.today()
    closed = 0
    for trade_id, entry_dt in cursor.fetchall():
        e = datetime.strptime(entry_dt[:10], '%Y-%m-%d').date()
        if (today - e).days > max_days:
            cursor.execute("UPDATE trades SET status = 'closed' WHERE id = ?", (trade_id,))
            closed += 1
    conn.commit()
    conn.close()
    if closed:
        print(f"Aged out {closed} trade(s) past {max_days} days.")


# ----------------------------------------------------------------------------
# peak + latest — refreshed per row, peak kept as a running BEST
# ----------------------------------------------------------------------------
def fill_latest_prices():
    """
    Refresh latest + peak for every ACTIVE trade that has an entry. Updates are
    keyed per row (id), and peak is a running best: we keep whichever of the
    stored vs freshly-computed peak is more favourable in the trade's direction
    (higher for long, lower for short). That makes peak monotonic even when the
    entry-day minute data expires.
    """
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, ticker, direction, entry_datetime, entry_method,
               peak_price, peak_datetime
        FROM trades
        WHERE status = 'active' AND entry_price IS NOT NULL
    """)
    rows = cursor.fetchall()

    for (trade_id, ticker, direction, entry_dt, entry_method,
         old_peak, old_peak_dt) in rows:
        stats = get_price_stats(ticker, entry_dt, entry_method, direction)
        if stats is None:
            continue

        peak_price = stats['peak_price']
        peak_dt = stats['peak_date']

        # running best: keep the stored peak if it's still the more favourable one
        if old_peak is not None:
            old_is_better = (old_peak >= peak_price) if direction == 'long' \
                            else (old_peak <= peak_price)
            if old_is_better:
                peak_price, peak_dt = old_peak, old_peak_dt

        cursor.execute("""
            UPDATE trades
            SET latest_price = ?, latest_datetime = ?, peak_price = ?, peak_datetime = ?
            WHERE id = ?
        """, (stats['latest_price'], stats['latest_date'], peak_price, peak_dt, trade_id))

        print(f"  {ticker} [{direction}] latest {stats['latest_price']:.2f}  peak {peak_price:.2f}")

    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------------
def show_cards():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ticker, source_group, direction, trade_type,
               insider_qty, insider_price, insider_value, filing_datetime,
               entry_price, entry_datetime, peak_price, peak_datetime,
               latest_price, latest_datetime, status
        FROM trades
        WHERE entry_price IS NOT NULL AND latest_price IS NOT NULL
    """)

    cards = []
    for (ticker, group, direction, ttype, iqty, iprice, ivalue, filing_dt,
         entry, entry_dt, peak, peak_dt, latest, latest_dt, status) in cursor.fetchall():
        cards.append({
            'ticker': ticker, 'group': group, 'direction': direction, 'ttype': ttype,
            'iqty': iqty, 'iprice': iprice, 'ivalue': ivalue, 'filing_dt': filing_dt,
            'entry': entry, 'entry_dt': entry_dt, 'peak': peak, 'peak_dt': peak_dt,
            'latest': latest, 'latest_dt': latest_dt, 'status': status,
            'cur_pct': pct_move(entry, latest, direction),
            'peak_pct': pct_move(entry, peak, direction),
            'days_to_peak': days_between(entry_dt, peak_dt),
            'tier': size_tier(entry),
        })
    conn.close()

    # best favourable move first
    cards.sort(key=lambda c: (c['peak_pct'] is not None, c['peak_pct']), reverse=True)

    for c in cards:
        tag = 'LONG ' if c['direction'] == 'long' else 'SHORT'
        frozen = '' if c['status'] == 'active' else '  (closed)'
        print(f"\n{c['ticker']:6} {tag} {c['group']:13} {c['tier']:9}{frozen}")
        print(f"  insider   {c['ttype']:12} qty {c['iqty']:>10}  @ {c['iprice']}  (${c['ivalue']:,.0f})")
        print(f"  filed     {c['filing_dt']}")
        print(f"  entry     {c['entry']:9.2f}  @ {c['entry_dt']}")
        print(f"  peak      {c['peak']:9.2f}  @ {c['peak_dt']}   {c['peak_pct']:+.2f}%"
              f"   ({c['days_to_peak']} d to peak)")
        print(f"  latest    {c['latest']:9.2f}  @ {c['latest_dt']}   {c['cur_pct']:+.2f}%")


def _stats_line(name, g):
    """Print one summary row: n, avg current %, avg peak %, avg days-to-peak."""
    n = len(g)
    avg_cur = sum(d['cur'] for d in g) / n
    avg_peak = sum(d['peak'] for d in g) / n
    dvals = [d['days'] for d in g if d['days'] is not None]
    avg_days = (sum(dvals) / len(dvals)) if dvals else None
    days_str = f"{avg_days:>10.1f}" if avg_days is not None else f"{'-':>10}"
    print(f"  {name:14} {n:>4}  {avg_cur:>+9.2f}  {avg_peak:>+9.2f}  {days_str}")


def _header(title):
    print(f"\n{title}")
    print(f"  {'':14} {'n':>4}  {'cur %':>9}  {'peak %':>9}  {'days→peak':>10}")


def _print_breakdown(title, data, key):
    buckets = {}
    for d in data:
        buckets.setdefault(d[key], []).append(d)
    _header(title)
    for name in sorted(buckets):
        _stats_line(name, buckets[name])


def show_averages():
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_group, direction, entry_price, entry_datetime,
               peak_price, peak_datetime, latest_price
        FROM trades
        WHERE entry_price IS NOT NULL AND latest_price IS NOT NULL
    """)
    data = []
    for group, direction, entry, entry_dt, peak, peak_dt, latest in cursor.fetchall():
        data.append({
            'group': group,
            'direction': direction,
            'tier': size_tier(entry),
            'cur': pct_move(entry, latest, direction),
            'peak': pct_move(entry, peak, direction),
            'days': days_between(entry_dt, peak_dt),
        })
    conn.close()

    if not data:
        print("\nNo priced trades to average yet.")
        return

    print("\ncur % = entry→latest, peak % = entry→best move (signed for direction)")
    print("days→peak = avg calendar days from buy-in (FPTP) to the best price")

    # whole-DB total across every priced trade
    _header("Overall (all priced trades):")
    _stats_line("ALL", data)

    _print_breakdown("By group:", data, 'group')
    _print_breakdown("By size tier:", data, 'tier')
    _print_breakdown("By direction:", data, 'direction')


# ----------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------
def run(update_only=False):
    """
    Full daily pass. update_only=True skips scrape+save and just refreshes
    prices + ages out (the 'manual price update at any time' path).
    """
    create_table()
    if not update_only:
        from collector import collect          # needs collector's module-level
        save_trades(collect())                 # scrape moved under __main__ (see note)
    fill_entry_prices()
    age_out(TEST_WINDOW_DAYS)
    fill_latest_prices()
    show_cards()
    show_averages()


if __name__ == '__main__':
    import sys
    run(update_only=('--update' in sys.argv))