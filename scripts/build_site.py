"""把 data/daily/*.json 組成網站需要的檔案：
  site/data/bars/XXXX.json   每檔最近 100 個交易日的日 K
  site/data/scan.json        全市場用預設規則模擬的結果（給掃描表）
  site/data/meta.json        資料日期、檔數、預設規則
  site/index.html            由 web/index.html 複製
"""
import json, os, shutil, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import bars_rel, sample_paths, run_rules, summarize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY = os.path.join(ROOT, 'data', 'daily')
SITE = os.path.join(ROOT, 'site')
KEEP_BARS = 100
# 預設規則（與網頁預設一致）
RULES = dict(sl=0.10, arm=0.10, max_hold=20, fee_discount=0.6)
WINDOW, SHRINK, N_PATHS = 60, 0.25, 4000
MIN_DAYS = 61  # 資料不足 61 個交易日的新股不掃（近期上市的暴衝會讓模擬過度樂觀）


def load_daily():
    files = sorted(f for f in os.listdir(DAILY) if f.endswith('.json'))
    stocks = {}  # code -> dict(name, market, bars=[[d,o,h,l,c,vol,amt]])
    for f in files:
        j = json.load(open(os.path.join(DAILY, f)))
        d = j['d']
        for market in ('tw', 'two'):
            for code, name, o, h, l, c, vol, amt in j[market]:
                s = stocks.setdefault(code, dict(name=name, market='TW' if market == 'tw' else 'TWO', bars=[]))
                s['name'] = name
                s['bars'].append([d, o, h, l, c, vol, amt])
    return stocks, files


def wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def ta_state(df):
    """最新一天的技術面：DMI(14)、RSI(14)、均線排列。回傳 dict 或 None（資料不足）。"""
    if len(df) < 61:
        return None
    h, l, c = df['High'], df['Low'], df['Close']
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    up = h - h.shift(1); dn = l.shift(1) - l
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    atr = wilder(tr, 14)
    pdi = 100 * wilder(plus_dm, 14) / atr
    mdi = 100 * wilder(minus_dm, 14) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = wilder(dx, 14)
    delta = c.diff(); gain = wilder(delta.clip(lower=0), 14); loss = wilder(-delta.clip(upper=0), 14)
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    ma = {n: c.rolling(n).mean().iloc[-1] for n in (5, 10, 20, 60)}
    last = float(c.iloc[-1])
    v = dict(pdi=float(pdi.iloc[-1]), mdi=float(mdi.iloc[-1]), adx=float(adx.iloc[-1]), adx3=float(adx.iloc[-4]),
             rsi=float(rsi.iloc[-1]), al=bool(ma[5] > ma[10] > ma[20] > ma[60]), a20=bool(last > ma[20]), a60=bool(last > ma[60]),
             d20=float(last / ma[20] - 1), r20=float(last / c.iloc[-21] - 1))
    if any(x != x for x in (v['pdi'], v['mdi'], v['adx'], v['rsi'])):
        return None
    if v['pdi'] < v['mdi'] and v['adx'] > 25: st = 'bear_strong'
    elif v['al']: st = 'bull_align'
    elif v['pdi'] > v['mdi'] and v['adx'] > 25: st = 'bull_strong'
    elif v['adx'] < 20: st = 'range'
    else: st = 'other'
    v['st'] = st
    return v


def main():
    t0 = time.time()
    stocks, files = load_daily()
    asof = files[-1][:8]
    asof_iso = f'{asof[:4]}-{asof[4:6]}-{asof[6:]}'
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    os.makedirs(os.path.join(SITE, 'data', 'bars'))
    shutil.copy(os.path.join(ROOT, 'web', 'index.html'), os.path.join(SITE, 'index.html'))
    rng = np.random.default_rng(int(asof))
    scan = []
    n_bars = 0
    for code, s in stocks.items():
        bars = s['bars'][-KEEP_BARS:]
        # 最後一天沒有成交的（停牌等）仍保留，但掃描時要求最新日期有資料
        json.dump(dict(code=code, name=s['name'], market=s['market'], asof=asof_iso,
                       bars=[[b[0], b[1], b[2], b[3], b[4]] for b in bars]),
                  open(os.path.join(SITE, 'data', 'bars', f'{code}.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
        n_bars += 1
        if len(bars) < MIN_DAYS or bars[-1][0] != asof_iso or '-創' in s['name'] or code.startswith('00'):
            continue  # 新股、創新板、ETF（00 開頭）不進掃描表；單檔查詢仍可用
        df = pd.DataFrame(bars, columns=['d', 'Open', 'High', 'Low', 'Close', 'Vol', 'Amt'])
        rel = bars_rel(df)
        if len(rel) < 30:
            continue
        buy = float(df['Close'].iloc[-1])
        window = min(WINDOW, len(rel))
        paths = sample_paths(rel, RULES['max_hold'] + 1, N_PATHS, window, 5, SHRINK, True, rng)
        r = summarize(run_rules(paths, buy, **RULES))
        amt20 = float(df['Amt'].tail(20).mean())
        rd = lambda x, k=3: None if x != x else round(float(x), k)  # NaN -> null
        ta = ta_state(df) or {}
        scan.append(dict(c=code, n=s['name'], m=s['market'], p=buy, a=round(amt20 / 1e6, 1),
                         sl=rd(r['P_停損']), tp=rd(r['P_停利']), to=rd(r['P_到期']),
                         w=rd(r['P_獲利']), ev=rd(r['EV'], 4), h=rd(r['avg_hold'], 1),
                         p5=rd(r['p5']), asl=rd(r['avg_停損']), atp=rd(r['avg_停利']),
                         st=ta.get('st'), pdi=rd(ta.get('pdi', float('nan')), 1), mdi=rd(ta.get('mdi', float('nan')), 1), adx=rd(ta.get('adx', float('nan')), 1),
                         rsi=rd(ta.get('rsi', float('nan')), 1), al=ta.get('al'), a20=ta.get('a20'), a60=ta.get('a60'), d20=rd(ta.get('d20', float('nan'))), r20=rd(ta.get('r20', float('nan')))))
    scan.sort(key=lambda x: x['sl'])
    json.dump(scan, open(os.path.join(SITE, 'data', 'scan.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    meta = dict(asof=asof_iso, days=len(files), stocks=n_bars, scanned=len(scan), rules=RULES,
                window=WINDOW, shrink=SHRINK, n_paths=N_PATHS, built=time.strftime('%Y-%m-%d %H:%M:%S'))
    json.dump(meta, open(os.path.join(SITE, 'data', 'meta.json'), 'w'), ensure_ascii=False)
    # 讓網頁知道資料不是內嵌的
    print(f'資料到 {asof_iso}，{len(files)} 個交易日，{n_bars} 檔日 K，掃描 {len(scan)} 檔，耗時 {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
