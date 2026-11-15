"""抓取上市（證交所）與上櫃（櫃買中心）全市場每日收盤行情，存成 data/daily/YYYYMMDD.json。
用法：
  python scripts/fetch_daily.py            # 補到今天為止、最近 N 個交易日（預設 110）
  python scripts/fetch_daily.py --days 5   # 只補最近 5 個交易日（每日排程用）
已存在的日期會跳過；假日（沒有資料）會記錄在 data/holidays.json 以免重複查詢。
"""
import argparse, json, os, re, sys, time
from datetime import date, timedelta
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY = os.path.join(ROOT, 'data', 'daily')
HOLIDAYS = os.path.join(ROOT, 'data', 'holidays.json')
os.makedirs(DAILY, exist_ok=True)
UA = 'tw-time-machine/1.0 (+https://github.com)'
THROTTLE = 4.0  # 秒；證交所對過快的請求會封鎖（曾經 2.5 秒連抓 20 天就被擋）


def get_json(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read().decode('utf-8', 'replace')
            time.sleep(THROTTLE)
            return json.loads(raw)
        except Exception as e:  # noqa
            last = e
            time.sleep(THROTTLE * (i + 2))
    print(f'  !! 失敗 {url}: {last}', file=sys.stderr)
    return None


def num(s):
    s = str(s).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_twse(d: date):
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json'
    j = get_json(url)
    if not j or j.get('stat') != 'OK':
        return None
    tabs = [t for t in j.get('tables', []) if t.get('title') and '每日收盤行情' in t['title']]
    if not tabs:
        return None
    t = tabs[0]
    ci = {f: i for i, f in enumerate(t['fields'])}
    rows = []
    for r in t['data']:
        code = r[ci['證券代號']].strip()
        if not re.fullmatch(r'\d{4}', code):
            continue
        o, h, l, c = (num(r[ci[k]]) for k in ('開盤價', '最高價', '最低價', '收盤價'))
        vol = num(r[ci['成交股數']]); amt = num(r[ci['成交金額']])
        if None in (o, h, l, c) or c <= 0:
            continue
        rows.append([code, r[ci['證券名稱']].strip(), o, h, l, c, int(vol or 0), int(amt or 0)])
    return rows


def fetch_tpex(d: date):
    url = f'https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={d:%Y/%m/%d}&type=EW&response=json'
    j = get_json(url)
    if not j:
        return None
    tabs = j.get('tables') or []
    if not tabs:
        return None            # 格式不對：當作錯誤
    if not tabs[0].get('data'):
        return []              # 櫃買明確回「沒有資料」：非交易日
    t = tabs[0]
    fields = [f.strip() for f in t['fields']]
    def idx(name):
        for i, f in enumerate(fields):
            if f.startswith(name):
                return i
        raise KeyError(name)
    ic, iname, iclose, iopen, ihigh, ilow, ivol, iamt = (idx(k) for k in ('代號', '名稱', '收盤', '開盤', '最高', '最低', '成交股數', '成交金額'))
    rows = []
    for r in t['data']:
        code = r[ic].strip()
        if not re.fullmatch(r'\d{4}', code):
            continue
        o, h, l, c = (num(r[i]) for i in (iopen, ihigh, ilow, iclose))
        vol = num(r[ivol]); amt = num(r[iamt])
        if None in (o, h, l, c) or c <= 0:
            continue
        rows.append([code, r[iname].strip(), o, h, l, c, int(vol or 0), int(amt or 0)])
    return rows

def bootstrap_from_yahoo(need_days: int):
    """資料庫還很空的時候（第一次部署），用 Yahoo 的批次資料一次補齊歷史日 K。
    股票清單來自最新一個官方快照（先抓到的那天）。成交金額以 成交股數×收盤 估計；之後每天的官方資料是精確的。"""
    try:
        import yfinance as yf
    except ImportError:
        print('  沒有 yfinance，略過 Yahoo 補歷史'); return 0
    files = sorted(f for f in os.listdir(DAILY) if f.endswith('.json'))
    if not files:
        print('  還沒有任何官方快照，無法取得股票清單'); return 0
    latest = json.load(open(os.path.join(DAILY, files[-1])))
    codes = {}
    for m in ('tw', 'two'):
        for r in latest[m]:
            codes[r[0]] = (r[1], m)
    tickers = [f"{c}.{'TW' if m == 'tw' else 'TWO'}" for c, (n, m) in codes.items()]
    n_tw = sum(1 for v in codes.values() if v[1] == 'tw'); n_two = len(codes) - n_tw
    print(f'  Yahoo 分批下載 {len(tickers)} 檔（約 5～8 分鐘）…', flush=True)

    def grab(batch, threads):
        """下載一批，回傳 {ticker: DataFrame}；失敗的不在裡面。"""
        out = {}
        try:
            raw = yf.download(batch, period='9mo', group_by='ticker', auto_adjust=False, progress=False, threads=threads)
        except Exception as e:
            print(f'  Yahoo 批次失敗：{e}'); return out
        for tk in batch:
            try:
                d = raw[tk].dropna(subset=['Open', 'High', 'Low', 'Close']) if len(batch) > 1 else raw.dropna(subset=['Open', 'High', 'Low', 'Close'])
            except Exception:
                continue
            if len(d) >= 20:
                out[tk] = d
        return out

    frames = {}
    for i in range(0, len(tickers), 150):           # 分批＋間隔，避免被 Yahoo 限流
        frames.update(grab(tickers[i:i + 150], 4))
        time.sleep(3)
    failed = [tk for tk in tickers if tk not in frames]
    for attempt in range(3):                          # 失敗的休息一下再重試
        if not failed:
            break
        print(f'  第 {attempt + 1} 次重試 {len(failed)} 檔…', flush=True)
        time.sleep(45)
        for i in range(0, len(failed), 40):
            frames.update(grab(failed[i:i + 40], 2))
            time.sleep(6)
        failed = [tk for tk in tickers if tk not in frames]
    print(f'  Yahoo 成功 {len(frames)} 檔，失敗 {len(failed)} 檔')

    have = {f[:8] for f in files}
    by_day = {}
    ok = len(frames)
    for tk, d in frames.items():
        code = tk.split('.')[0]; name, m = codes[code]
        for idx, r in d.iterrows():
            key = idx.strftime('%Y%m%d')
            vol = r['Volume']; vol = 0 if vol != vol else int(vol)
            if key in have or float(r['Close']) <= 0 or vol == 0:
                continue  # 已有官方資料的日期不覆蓋；Yahoo 在休市日會出現成交量 0 的假 K 棒
            by_day.setdefault(key, {'tw': [], 'two': []})[m].append(
                [code, name, round(float(r['Open']), 2), round(float(r['High']), 2), round(float(r['Low']), 2),
                 round(float(r['Close']), 2), vol, int(vol * float(r['Close']))])
    written = 0
    for key in sorted(by_day)[-need_days:]:
        v = by_day[key]
        if len(v['tw']) < 0.8 * n_tw or len(v['two']) < 0.8 * n_two:
            print(f'  {key} 只有上市 {len(v["tw"])}／上櫃 {len(v["two"])} 檔，略過'); continue  # 太少檔的日子通常是 Yahoo 資料不完整
        json.dump({'d': f'{key[:4]}-{key[4:6]}-{key[6:]}', 'tw': v['tw'], 'two': v['two'], 'src': 'yahoo'},
                  open(os.path.join(DAILY, key + '.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
        written += 1
    print(f'  Yahoo 有資料 {ok} 檔，補了 {written} 個交易日')
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=110, help='要確保存在的最近交易日數')
    ap.add_argument('--max-back', type=int, default=200, help='最多往回看幾個日曆日')
    ap.add_argument('--max-requests', type=int, default=12, help='單次執行最多向官方抓幾個交易日（太多會被證交所封鎖，剩下的下次再補）')
    a = ap.parse_args()
    holidays = set(json.load(open(HOLIDAYS))) if os.path.exists(HOLIDAYS) else set()
    have = {f[:8] for f in os.listdir(DAILY) if f.endswith('.json')}
    today = date.today()
    # 第一次部署：先確保有最新一天的官方快照，再用 Yahoo 補歷史
    if len(have) < 60:
        d = today
        for _ in range(10):
            key = f'{d:%Y%m%d}'
            if d.weekday() < 5 and key not in have and key not in holidays:
                tw = fetch_twse(d); two = fetch_tpex(d)
                if tw and two:
                    json.dump({'d': d.isoformat(), 'tw': tw, 'two': two}, open(os.path.join(DAILY, key + '.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
                    have.add(key); print(f'  最新官方快照 {d}：上市 {len(tw)} 檔、上櫃 {len(two)} 檔'); break
            d -= timedelta(days=1)
        if 0 < len(have) < 60:
            bootstrap_from_yahoo(a.days)
            have = {f[:8] for f in os.listdir(DAILY) if f.endswith('.json')}
    budget = a.max_requests
    got = 0; d = today
    for _ in range(a.max_back):
        key = f'{d:%Y%m%d}'
        if d.weekday() < 5 and key not in holidays:
            if key in have:
                got += 1
            else:
                if budget <= 0:
                    print(f'  本次額度用完，{d} 之前的缺漏下次再補'); break
                budget -= 1
                print(f'抓取 {d} …', flush=True)
                tw = fetch_twse(d)
                two = fetch_tpex(d)
                if two is None:
                    print(f'  !! {d} 櫃買中心查詢失敗，下次再抓', file=sys.stderr)
                    d -= timedelta(days=1); continue
                if tw is None and two == []:
                    # 兩邊都沒資料：過去的日期視為假日；今天則可能還沒公布
                    if (today - d).days >= 1:
                        holidays.add(key)
                        print(f'  {d} 無資料，記為非交易日')
                    else:
                        print(f'  {d} 尚無資料（可能還沒公布）')
                elif tw is None or not two:
                    print(f'  !! {d} 只抓到一邊（上市 {0 if tw is None else len(tw)}、上櫃 {len(two)}），不存檔，下次再抓', file=sys.stderr)
                else:
                    if True:
                        json.dump({'d': d.isoformat(), 'tw': tw, 'two': two}, open(os.path.join(DAILY, key + '.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
                        print(f'  上市 {len(tw)} 檔、上櫃 {len(two)} 檔')
                        got += 1
            if got >= a.days:
                break
        d -= timedelta(days=1)
    json.dump(sorted(holidays), open(HOLIDAYS, 'w'))
    # 只保留最近 140 個交易日的檔案
    files = sorted(f for f in os.listdir(DAILY) if f.endswith('.json'))
    for f in files[:-140]:
        os.remove(os.path.join(DAILY, f))
    print(f'完成：目前共有 {min(len(files),140)} 個交易日')


if __name__ == '__main__':
    main()
