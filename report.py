"""
Standalone HTML report for TradeTracker.

Reads the trades DB and writes a self-contained, sortable report.html
(no server, no external dependencies). Pulls its shared helpers from store.py
so the %/tier/day-gap logic lives in exactly one place.
"""
from datetime import datetime

from store import connect, pct_move, size_tier, days_between, value_bucket

REPORT_PATH = 'report.html'

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1d24;--mut:#6b7280;--line:#e5e7eb;
--pos:#0a7d3c;--posbg:#e7f6ee;--neg:#c0392b;--negbg:#fdecea;--long:#1d4ed8;--short:#c2410c;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}
header{padding:20px 24px;background:var(--card);border-bottom:1px solid var(--line);}
h1{margin:0 0 4px;font-size:18px;letter-spacing:.2px}
.sub{color:var(--mut);font-size:13px}
.wrap{padding:20px 24px}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h2{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
.card table{width:100%;border-collapse:collapse;font-size:13px}
.card td,.card th{padding:3px 4px;text-align:right;white-space:nowrap}
.card th:first-child,.card td:first-child{text-align:left}
.card thead th{color:var(--mut);font-weight:600;border-bottom:1px solid var(--line)}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tbl-scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table.trades{width:100%;border-collapse:collapse;font-size:13px}
table.trades th,table.trades td{padding:8px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
table.trades th:first-child,table.trades td:first-child,
table.trades th.l,table.trades td.l{text-align:left}
table.trades thead th{position:sticky;top:0;background:var(--card);cursor:pointer;
user-select:none;font-size:12px;color:var(--mut);border-bottom:2px solid var(--line)}
table.trades thead th:hover{color:var(--ink)}
table.trades thead th .ar{opacity:.35;font-size:10px;margin-left:3px}
table.trades tbody tr:hover{background:#fafbfc}
tr.closed{opacity:.5}
.sub2{display:block;font-size:11px;color:var(--mut)}
.pos{color:var(--pos)} .neg{color:var(--neg)}
.pct{font-weight:600;border-radius:5px;padding:2px 6px;font-variant-numeric:tabular-nums}
.pct.pos{background:var(--posbg)} .pct.neg{background:var(--negbg)}
.badge{font-size:11px;font-weight:700;letter-spacing:.4px;padding:2px 7px;border-radius:20px}
.badge.long{color:var(--long);background:#e6edfe} .badge.short{color:var(--short);background:#fdeee2}
"""

_JS = """
function sortTable(th){
 var table=th.closest('table'), idx=[].indexOf.call(th.parentNode.children,th);
 var asc=!(th.dataset.asc==='1'); [].forEach.call(th.parentNode.children,function(h){h.removeAttribute('data-asc');h.querySelector('.ar')&&(h.querySelector('.ar').textContent='');});
 th.dataset.asc=asc?'1':'0'; var ar=th.querySelector('.ar'); if(ar)ar.textContent=asc?'\\u25B2':'\\u25BC';
 var tb=table.tBodies[0], rows=[].slice.call(tb.rows);
 rows.sort(function(a,b){
   var x=a.cells[idx].dataset.sort, y=b.cells[idx].dataset.sort;
   var nx=parseFloat(x), ny=parseFloat(y), num=(x!==''&&y!==''&&!isNaN(nx)&&!isNaN(ny));
   if(num){return asc?nx-ny:ny-nx;}
   x=(x||a.cells[idx].textContent).toLowerCase(); y=(y||b.cells[idx].textContent).toLowerCase();
   return asc?(x<y?-1:x>y?1:0):(x>y?-1:x<y?1:0);
 });
 rows.forEach(function(r){tb.appendChild(r);});
}
"""


def _tier_rank(t):
    return {'<$1': 0, '$1-4.99': 1, '$5-14.99': 5, '$15-49.99': 15, '$50+': 50}.get(t, 999)


def _dedupe(trades):
    """
    One row per real trade. Per (ticker, trade_date, trade_type): keep the
    individual-page rows (de-duplicated by quantity); keep cluster rows only if
    that event has no individual row at all. Different quantities are treated as
    genuinely different trades and kept separate.
    """
    groups = {}
    for t in trades:
        groups.setdefault((t['tk'], t['td'], t['tt']), []).append(t)
    out = []
    for _, g in groups.items():
        indiv = [t for t in g if t['grp'] != 'cluster_buys']
        pool = indiv if indiv else g
        seen = set()
        for t in pool:
            if t['iq'] in seen:
                continue
            seen.add(t['iq'])
            out.append(t)
    return out


def _pct_cell(v):
    if v is None:
        return '<td class="mono" data-sort=""></td>'
    cls = 'pos' if v >= 0 else 'neg'
    return f'<td class="mono" data-sort="{v:.4f}"><span class="pct {cls}">{v:+.2f}%</span></td>'


def _price_cell(price, when):
    sub = f'<span class="sub2">@ {when}</span>' if when else ''
    return f'<td class="mono" data-sort="{price:.4f}">{price:.2f}{sub}</td>'


def _summary_table(title, data, key, keyfn=None):
    buckets = {}
    for d in data:
        buckets.setdefault(d[key], []).append(d)
    order = sorted(buckets, key=keyfn) if keyfn else sorted(buckets)
    rows = ''
    for name in order:
        g = buckets[name]
        n = len(g)
        ac = sum(d['cur'] for d in g) / n
        ap = sum(d['peak'] for d in g) / n
        dv = [d['days'] for d in g if d['days'] is not None]
        ad = sum(dv) / len(dv) if dv else None
        ac_c = 'pos' if ac >= 0 else 'neg'
        ap_c = 'pos' if ap >= 0 else 'neg'
        days = f'{ad:.1f}' if ad is not None else '-'
        rows += (f'<tr><td>{name}</td><td class="mono">{n}</td>'
                 f'<td class="mono {ac_c}">{ac:+.2f}</td><td class="mono {ap_c}">{ap:+.2f}</td>'
                 f'<td class="mono">{days}</td></tr>')
    return (f'<div class="card"><h2>{title}</h2><table><thead><tr>'
            f'<th>bucket</th><th>n</th><th>cur%</th><th>peak%</th><th>d&rarr;pk</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')


def write_report(path=REPORT_PATH):
    conn = connect()
    cur = conn.cursor()
    cur.execute("""SELECT ticker,source_group,direction,trade_type,insider_qty,insider_price,
        insider_value,filing_datetime,trade_date,entry_price,entry_datetime,peak_price,
        peak_datetime,latest_price,latest_datetime,status FROM trades
        WHERE entry_price IS NOT NULL AND latest_price IS NOT NULL""")
    trades = []
    for (tk, grp, dr, tt, iq, ip, iv, fdt, td, en, edt, pk, pdt, lt, ldt, st) in cur.fetchall():
        trades.append(dict(tk=tk, grp=grp, dr=dr, tt=tt, iq=iq, ip=ip, iv=iv, fdt=fdt, td=td,
            en=en, edt=edt, pk=pk, pdt=pdt, lt=lt, ldt=ldt, st=st,
            cur=pct_move(en, lt, dr), peak=pct_move(en, pk, dr),
            days=days_between(edt, pdt), tier=size_tier(en),
            vbucket=value_bucket(iv)))
    cur.execute("SELECT COUNT(*) FROM trades WHERE entry_price IS NULL")
    pending = cur.fetchone()[0]
    conn.close()

    # one row per real trade for every view except By-group (which keeps all rows)
    uniq = _dedupe(trades)

    agg = [dict(group=t['grp'], direction=t['dr'], tier=t['tier'],
                vbucket=t['vbucket'], cur=t['cur'], peak=t['peak'], days=t['days'])
           for t in uniq]
    agg_all = [dict(group=t['grp'], cur=t['cur'], peak=t['peak'], days=t['days'])
               for t in trades]
    summaries = ''
    if agg:
        summaries += _summary_table('Overall', [{**a, 'group': 'ALL'} for a in agg], 'group')
        summaries += _summary_table('By group', agg_all, 'group')
        summaries += _summary_table('By size tier', agg, 'tier', keyfn=_tier_rank)
        summaries += _summary_table('By direction', agg, 'direction')
        summaries += _summary_table('By value', agg, 'vbucket')

    uniq.sort(key=lambda t: (t['peak'] is not None, t['peak']), reverse=True)
    body = ''
    for t in uniq:
        rowcls = ' class="closed"' if t['st'] != 'active' else ''
        badge = f'<span class="badge {t["dr"]}">{t["dr"].upper()}</span>'
        body += (f'<tr{rowcls}>'
            f'<td class="l mono"><b>{t["tk"]}</b></td>'
            f'<td class="l" data-sort="{t["dr"]}">{badge}</td>'
            f'<td class="l">{t["grp"]}</td>'
            f'<td class="l" data-sort="{_tier_rank(t["tier"])}">{t["tier"]}</td>'
            f'<td class="l">{t["tt"]}</td>'
            f'<td class="mono" data-sort="{t["iq"]}">{t["iq"]:,}</td>'
            f'<td class="mono" data-sort="{t["iv"]}">${t["iv"]:,.0f}</td>'
            f'<td class="l mono" data-sort="{t["fdt"]}">{t["fdt"]}</td>'
            f'{_price_cell(t["en"], t["edt"])}'
            f'{_price_cell(t["pk"], t["pdt"])}'
            f'{_pct_cell(t["peak"])}'
            f'{_price_cell(t["lt"], t["ldt"])}'
            f'{_pct_cell(t["cur"])}'
            f'<td class="mono" data-sort="{t["days"] if t["days"] is not None else -1}">'
            f'{t["days"] if t["days"] is not None else "-"}</td>'
            f'<td class="l">{t["st"]}</td></tr>')

    heads = [('Ticker', 'l'), ('Dir', 'l'), ('Group', 'l'), ('Tier', 'l'), ('Type', 'l'),
             ('Insider Qty', ''), ('Insider $', ''), ('Filed', 'l'), ('Entry', ''), ('Peak', ''),
             ('Peak %', ''), ('Latest', ''), ('Cur %', ''), ('D&rarr;Peak', ''), ('Status', 'l')]
    thead = ''.join(
        f'<th class="{c}" onclick="sortTable(this)">{n}<span class="ar"></span></th>'
        for n, c in heads)

    gen = datetime.now().strftime('%Y-%m-%d %H:%M')
    pend_line = f' &middot; {pending} pending entry (awaiting next open)' if pending else ''
    html = (f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>TradeTracker Report</title><style>{_CSS}</style></head><body>'
        f'<header><h1>TradeTracker &mdash; Insider Trade Report</h1>'
        f'<div class="sub">Generated {gen} &middot; {len(uniq)} unique trades{pend_line}</div></header>'
        f'<div class="wrap"><div class="summary">{summaries}</div>'
        f'<div class="tbl-scroll"><table class="trades"><thead><tr>{thead}</tr></thead>'
        f'<tbody>{body}</tbody></table></div></div>'
        f'<script>{_JS}</script></body></html>')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Wrote {path} ({len(uniq)} unique of {len(trades)} rows).")


if __name__ == '__main__':
    write_report()