from bs4 import BeautifulSoup
import requests

SOURCES = { #Dictrionary of page links.
    'cluster_buys': 'http://openinsider.com/latest-cluster-buys',
    'insider_buys': 'http://openinsider.com/insider-purchases-25k',
    'penny_buys': 'http://openinsider.com/latest-penny-stocks-buys',

}

# One reusable session that ignores the system proxy settings.
session = requests.Session()
session.trust_env = False

def fetch_html(url):
    """Download a page and hand back its raw HTML as text."""
    response = session.get(url)      # note: session.get, not requests.get
    response.raise_for_status()
    return response.text

def parse_trades(html, group):
    """Turn one page's HTML into a list of trade dictionaries."""
    soup = BeautifulSoup(html, 'html.parser') #Builds parseable soup.
    table = soup.find('table', class_='tinytable') #Finds first match.
    rows = table.find_all('tr') #Returns list of all rows.

    trades = []
    for row in rows:
        cells = row.find_all('td')
        if len(cells) < 13: # skip header and non data rows
            continue
        trade = {
            'group': group,
            'filing_date': cells[1].get_text(strip=True),
            'trade_date':  cells[2].get_text(strip=True),
            'ticker':      cells[3].get_text(strip=True),
            'trade_type':  cells[7].get_text(strip=True),
            'price':       cells[8].get_text(strip=True),
            'qty':         cells[9].get_text(strip=True),
            'value':       cells[12].get_text(strip=True),
        }
        trades.append(trade)

    return trades[:10] #First 10

def collect():
    """Run fetch + parse over all 3 sources and combine"""
    all_trades = []
    for group, url in SOURCES.items():
        html = fetch_html(url)
        trades = parse_trades(html, group)
        all_trades.extend(trades)
    return all_trades

if __name__ == '__main__':
    trades = collect()
    for t in trades:
        print(t)

    print(f"\nCollected {len(trades)} trades.")