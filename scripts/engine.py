"""台股時光機模擬引擎
規則（依使用者設定）：
  - 停損：跌破 買價×(1-sl) 時盤中觸價出場（開盤已跌破則以開盤價出場）
  - 停利：收盤獲利 ≥ arm 之後「武裝」，之後任何一天收盤沒有高於前一天收盤 → 隔天開盤賣出
  - 到期：持有超過 max_hold 天仍未出場 → 隔天開盤賣出
  - 費用：手續費 0.1425%×折扣（買賣各一次）＋證交稅 0.3%（賣出）
路徑產生：區塊重抽（block bootstrap）近 window 天的 OHLC 日 K（相對前收），
  保留波動叢聚；漂移（drift）依 shrink 收縮以避免近期走勢過度外推；±10% 漲跌停截斷。
"""
import numpy as np
import pandas as pd

FEE = 0.001425
TAX = 0.003
LIMIT = 0.10


def bars_rel(df: pd.DataFrame) -> np.ndarray:
    """回傳 (n,4) 的 [o,h,l,c] 相對前一日收盤的比率。"""
    pc = df['Close'].shift(1)
    rel = np.column_stack([
        (df['Open'] / pc).values, (df['High'] / pc).values,
        (df['Low'] / pc).values, (df['Close'] / pc).values])
    rel = rel[1:]
    rel = rel[~np.isnan(rel).any(axis=1)]
    return rel


def sample_paths(rel: np.ndarray, horizon: int, n_paths: int, window: int = 120,
                 block: int = 5, drift_shrink: float = 0.25, vol_adjust: bool = True,
                 rng=None) -> np.ndarray:
    """回傳 (n_paths, horizon, 4) 的相對日 K。"""
    rng = rng or np.random.default_rng()
    src = rel[-window:]
    n = len(src)
    logc = np.log(src[:, 3])
    mu = logc.mean()
    # 漂移收縮：只保留 drift_shrink 比例的近期漂移
    adj = np.exp(-(1 - drift_shrink) * mu)
    # 波動調整：讓近 20 日波動與取樣窗口一致（限制在 0.6~1.6 倍）
    scale = 1.0
    if vol_adjust and n > 40:
        v_recent = np.log(rel[-20:, 3]).std()
        v_win = logc.std()
        if v_win > 0:
            scale = float(np.clip(v_recent / v_win, 0.6, 1.6))
    n_blocks = int(np.ceil(horizon / block))
    starts = rng.integers(0, n - block + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_paths, -1)[:, :horizon]
    paths = src[idx] * adj  # (n_paths, horizon, 4)
    if scale != 1.0:
        paths = np.exp(np.log(paths) * scale)
    # 漲跌停截斷
    paths = np.clip(paths, 1 - LIMIT, 1 + LIMIT)
    # 保持 h ≥ max(o,c), l ≤ min(o,c)
    paths[:, :, 1] = np.maximum(paths[:, :, 1], np.maximum(paths[:, :, 0], paths[:, :, 3]))
    paths[:, :, 2] = np.minimum(paths[:, :, 2], np.minimum(paths[:, :, 0], paths[:, :, 3]))
    return paths


def run_rules(paths: np.ndarray, buy: float, sl: float = 0.10, arm: float = 0.10,
              max_hold: int = 20, fee_discount: float = 0.6):
    """對 (n, horizon, 4) 的相對日 K 套用規則。horizon 需 ≥ max_hold+1。
    回傳 dict：exit_type(0=停損,1=停利,2=到期), net_ret, hold_days, sell_price"""
    n, horizon, _ = paths.shape
    fee = FEE * fee_discount
    # 絕對價格
    pc = np.full(n, buy, dtype=float)
    o = np.empty((n, horizon)); h = np.empty((n, horizon)); l = np.empty((n, horizon)); c = np.empty((n, horizon))
    for t in range(horizon):
        o[:, t] = pc * paths[:, t, 0]; h[:, t] = pc * paths[:, t, 1]
        l[:, t] = pc * paths[:, t, 2]; c[:, t] = pc * paths[:, t, 3]
        pc = c[:, t]
    sl_price = buy * (1 - sl)
    arm_price = buy * (1 + arm)
    exit_type = np.full(n, -1); sell = np.zeros(n); hold = np.zeros(n, dtype=int)
    armed = np.zeros(n, dtype=bool)
    pending_sell = np.zeros(n, dtype=bool)  # 今天開盤要賣
    prev_c = np.full(n, buy)
    alive = np.ones(n, dtype=bool)
    for t in range(horizon):
        day = t + 1
        # 1) 排定的隔天開盤賣出（停利/到期）
        m = alive & pending_sell
        if m.any():
            sell[m] = o[m, t]; hold[m] = day; alive[m] = False
        # 2) 盤中停損
        m = alive & (l[:, t] <= sl_price)
        if m.any():
            sell[m] = np.minimum(o[m, t], sl_price); exit_type[m] = 0; hold[m] = day; alive[m] = False
        # 3) 收盤判定：武裝 / 沒續漲 → 排定隔天賣
        m = alive
        newly = m & (~armed) & (c[:, t] >= arm_price)
        no_rise = m & armed & (c[:, t] <= prev_c)
        armed |= newly
        pending_sell[no_rise] = True; exit_type[no_rise] = 1
        # 4) 到期：持有滿 max_hold 天 → 隔天開盤賣
        if day >= max_hold:
            m2 = m & ~pending_sell
            pending_sell[m2] = True; exit_type[m2] = 2
        prev_c = c[:, t]
        if not alive.any():
            break
    # 仍存活（horizon 不足）→ 以最後收盤出場
    if alive.any():
        sell[alive] = c[alive, -1]; hold[alive] = horizon; exit_type[alive] = 2
    net = sell * (1 - fee - TAX) / (buy * (1 + fee)) - 1
    return dict(exit_type=exit_type, net_ret=net, hold_days=hold, sell_price=sell)


def summarize(res, exit_names=('停損', '停利', '到期')):
    et, net, hd = res['exit_type'], res['net_ret'], res['hold_days']
    out = {}
    for k, name in enumerate(exit_names):
        m = et == k
        out[f'P_{name}'] = float(m.mean())
        out[f'avg_{name}'] = float(net[m].mean()) if m.any() else float('nan')
    out['P_獲利'] = float((net > 0).mean())
    out['EV'] = float(net.mean())
    out['median'] = float(np.median(net))
    out['p5'] = float(np.percentile(net, 5))
    out['p95'] = float(np.percentile(net, 95))
    out['avg_hold'] = float(hd.mean())
    return out


def simulate(df: pd.DataFrame, buy: float = None, n_paths: int = 10000, window: int = 120,
             sl: float = 0.10, arm: float = 0.10, max_hold: int = 20, fee_discount: float = 0.6,
             drift_shrink: float = 0.25, vol_adjust: bool = True, block: int = 5, seed=None):
    rel = bars_rel(df)
    buy = float(buy if buy is not None else df['Close'].iloc[-1])
    rng = np.random.default_rng(seed)
    paths = sample_paths(rel, max_hold + 1, n_paths, window, block, drift_shrink, vol_adjust, rng)
    res = run_rules(paths, buy, sl, arm, max_hold, fee_discount)
    s = summarize(res)
    s['buy'] = buy
    return s, res


def actual_outcome(df: pd.DataFrame, t0: int, buy: float, **kw):
    """用 t0 之後的真實日 K 套用同一套規則（t0 為資料索引位置；買價為 t0+1 開盤）。"""
    max_hold = kw.get('max_hold', 20)
    fut = df.iloc[t0 + 1: t0 + 2 + max_hold]
    if len(fut) < max_hold + 1:
        return None
    pc = np.concatenate([[buy], fut['Close'].values[:-1]])
    rel = np.column_stack([fut['Open'].values / pc, fut['High'].values / pc,
                           fut['Low'].values / pc, fut['Close'].values / pc])[None, :, :]
    res = run_rules(rel, buy, kw.get('sl', 0.10), kw.get('arm', 0.10), max_hold, kw.get('fee_discount', 0.6))
    return dict(exit_type=int(res['exit_type'][0]), net_ret=float(res['net_ret'][0]), hold_days=int(res['hold_days'][0]))
