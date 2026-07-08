"""
买卖点策略回测对比：在持仓历史上比较三套规则的收益/回撤/胜率。
  NEW   : 三层 = 趋势门(收>MA200且MA200上升) + 回调择时(近MA50 且 RSI2<15) + 波动率止损(Chandelier近22高-3×波动率, 单调上移; 破MA200硬止损)
  OLD   : 现行 = 买:近60高×(1-10%); 卖:入场价×(1-12%)止损 或 ×(1+25%)止盈
  HOLD  : 买入持有基准
纯收盘价实现（库里无 OHLC）：ATR 用日收益率滚动标准差×价 代理。
执行：信号在 t 日收盘产生，t+1 日起持有（避免未来函数）。计单边成本。
用法：ssh 到服务器 docker run 里 python - < bt_levels.py
"""
import os
import numpy as np
import pandas as pd

import config
import data

COST = config.COMMISSION + config.SLIPPAGE  # 单边成本≈0.00125
CHAND_MULT = float(os.environ.get("CHAND_MULT", "3.0"))  # Chandelier 波动率倍数（可环境变量覆盖，用于调参扫描）
VOL_TO_ATR = 1.4      # 收益率std → ATR 的粗略换算（ATR 通常大于收到收std）
RSI_BUY = 15          # RSI(2) 超卖阈值（放宽到15以获取足够样本）
MA50_BUF = 0.5        # 买点：价 ≤ MA50 + 0.5×ATR代理


def rsi(s, period=2):
    d = s.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    rs = up.ewm(alpha=1 / period, adjust=False).mean() / dn.ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + rs)


def indicators(s):
    df = pd.DataFrame({"close": s})
    df["ret"] = df["close"].pct_change()
    df["ma50"] = df["close"].rolling(50).mean()
    df["ma200"] = df["close"].rolling(200).mean()
    df["ma200_slope"] = df["ma200"] - df["ma200"].shift(20)
    df["rsi2"] = rsi(df["close"], 2)
    df["high60"] = df["close"].rolling(60).max()
    df["high22"] = df["close"].rolling(22).max()
    df["vol"] = df["ret"].rolling(22).std()
    df["atr"] = df["vol"] * df["close"] * VOL_TO_ATR
    return df


def simulate(df, rule):
    """返回 (strat_daily_returns Series, trades list[win_bool])。次日执行。"""
    n = len(df)
    pos = 0
    entry = None
    chand = None
    pos_series = np.zeros(n)      # 第 i 日是否持有（用于累计 ret[i]）
    trans = np.zeros(n)           # 第 i 日是否发生买/卖（计成本）
    trades = []
    c = df["close"].values
    for i in range(n):
        row = df.iloc[i]
        # 有持仓：更新移动止损
        if pos == 1:
            raw = row["high22"] - CHAND_MULT * row["atr"]
            chand = raw if chand is None else max(chand, raw)  # 单调上移
        signal_exit = signal_entry = False
        if pos == 0:
            if rule == "NEW":
                regime = (c[i] > row["ma200"]) and (row["ma200_slope"] > 0)
                pull = c[i] <= row["ma50"] + MA50_BUF * row["atr"]
                signal_entry = bool(regime and pull and row["rsi2"] < RSI_BUY)
            elif rule == "LIVE":  # 线上逻辑：趋势门 + 近60高回撤10% 买入
                regime = (c[i] > row["ma200"]) and (row["ma200_slope"] > 0)
                signal_entry = bool(regime and (c[i] <= row["high60"] * 0.90))
            elif rule == "OLD":
                signal_entry = c[i] <= row["high60"] * 0.90
            elif rule == "REGIME":
                signal_entry = (c[i] > row["ma200"]) and (row["ma200_slope"] > 0)
            elif rule == "HOLD":
                signal_entry = not np.isnan(row["ma200"])  # 数据就绪即买入并持有
        else:
            if rule == "NEW":
                signal_exit = (c[i] < chand) or (c[i] < row["ma200"])
            elif rule == "LIVE":  # 线上逻辑：Chandelier 移动止损（近22高-3×ATR，每日重算不锁定）
                signal_exit = bool(c[i] < row["high22"] - CHAND_MULT * row["atr"])
            elif rule == "OLD":
                signal_exit = (c[i] <= entry * 0.88) or (c[i] >= entry * 1.25)
            elif rule == "REGIME":
                signal_exit = (c[i] < row["ma200"]) or (row["ma200_slope"] <= 0)
            elif rule == "HOLD":
                signal_exit = False
        # 次日执行
        if pos == 0 and signal_entry:
            pos = 1; entry = c[i]; trans[i] = 1
            chand = (row["high22"] - CHAND_MULT * row["atr"]) if rule == "NEW" else None
        elif pos == 1 and signal_exit:
            trades.append(c[i] > entry)   # 平仓：本次是否盈利
            pos = 0; entry = None; chand = None; trans[i] = 1
        pos_series[i] = pos
    # 收益：第 i 日持仓状态吃 ret[i+1]
    ret = df["ret"].values
    held = np.concatenate([[0], pos_series[:-1]])  # 昨日持仓吃今日收益
    strat = held * np.nan_to_num(ret)
    cost = np.concatenate([[0], trans[:-1]]) * COST  # 转仓次日扣成本（近似）
    strat = strat - cost
    return pd.Series(strat, index=df.index), trades


def metrics(strat_ret, trades):
    eq = (1 + strat_ret).cumprod()
    total = eq.iloc[-1] - 1
    n = len(strat_ret)
    cagr = (eq.iloc[-1]) ** (252 / n) - 1 if n > 20 and eq.iloc[-1] > 0 else float("nan")
    dd = (eq / eq.cummax() - 1).min()
    wins = sum(1 for w in trades if w)
    wr = wins / len(trades) if trades else float("nan")
    exposure = (strat_ret != 0).mean()
    return {"total": total, "cagr": cagr, "maxdd": dd, "winrate": wr,
            "trades": len(trades), "exposure": exposure}


def main():
    tickers = config.PORTFOLIO_DEFAULT
    rules = ["LIVE", "REGIME", "OLD", "HOLD"]
    port = {r: [] for r in rules}       # 组合日收益（等权）
    print(f"{'股票':<10}{'规则':<6}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'胜率':>7}{'交易':>5}{'仓位%':>7}")
    print("-" * 66)
    for t in tickers:
        name = config.TICKER_NAMES.get(t, t)
        s = data.load_combined_series(t).dropna()
        if len(s) < 220:
            print(f"{name:<10}数据不足({len(s)}日) 跳过")
            continue
        df = indicators(s)
        for r in rules:
            sr, tr = simulate(df, r)
            m = metrics(sr, tr)
            port[r].append(sr)
            wr = "-" if np.isnan(m["winrate"]) else f"{m['winrate']:.0%}"
            print(f"{name:<10}{r:<6}{m['total']:>+8.0%}{m['cagr']:>+8.1%}"
                  f"{m['maxdd']:>+9.0%}{wr:>7}{m['trades']:>5}{m['exposure']:>6.0%}")
        print("-" * 66)
    # 等权组合
    print(f"\n{'=== 等权组合（6只平均日收益）===':<40}")
    print(f"{'规则':<6}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'仓位%':>7}")
    for r in rules:
        allsr = pd.concat(port[r], axis=1)
        pr = allsr.mean(axis=1).fillna(0)
        m = metrics(pr, [])
        print(f"{r:<6}{m['total']:>+8.0%}{m['cagr']:>+8.1%}{m['maxdd']:>+9.0%}{m['exposure']:>6.0%}")


if __name__ == "__main__":
    main()
