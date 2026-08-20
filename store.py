import sqlite3
from datetime import date

DB_PATH = 'trades.db'

def connect():
    return sqlite3.connect(DB_PATH) # Open/Create the db file and return connection.

def create_table():
    conn = connect() 
    cursor = conn.cursor() # Actually runs statements:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_group    TEXT,
            filing_date     TEXT,
            trade_date      TEXT,
            ticker          TEXT,
            trade_type      TEXT,
            price           REAL,
            qty             INTEGER,
            value           REAL,
            status          TEXT DEFAULT 'active',
            first_seen      TEXT,
            UNIQUE(ticker, trade_date, trade_type, qty, value)
        )
    ''')
    conn.commit() # Actually makes change.
    conn.close() # Releases file.


def clean_number(text):
    cleaned = text.replace('$', '').replace(',', '').replace('+', '').strip()
    if cleaned == '':
        return None
    return float(cleaned)


def save_trades(trades):
    conn = connect()
    cursor = conn.cursor()
    today = date.today().isoformat() #ISO

    inserted = 0
    for t in trades:
        cursor.execute('''
            INSERT OR IGNORE INTO trades
                (source_group, filing_date, trade_date, ticker,
                 trade_type, price, qty, value, first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            t['group'],
            t['filing_date'],
            t['trade_date'],
            t['ticker'],
            t['trade_type'],
            clean_number(t['price']),
            clean_number(t['qty']),
            clean_number(t['value']),
            today,
        ))
        inserted += cursor.rowcount  # 1 if the row went in, 0 if ignored

    conn.commit()
    conn.close()
    print(f"Saved {inserted} new trades ({len(trades)} seen).")


def read_trades():
    """Return every stored trade as a list of rows."""
    conn = connect()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM trades ORDER BY filing_date DESC')
    rows = cursor.fetchall()
    conn.close()
    return rows

if __name__ == '__main__': # safe import
    from collector import collect
    create_table()
    trades = collect()
    save_trades(trades)
    for row in read_trades():
        print(row)
