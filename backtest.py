"""
回测引擎（纯 pandas 向量化）+ 绩效指标。

回测模型（简化但真实）：
- 在调仓日收盘按目标权重持仓，持有到下一个调仓日。
- 每个持仓期的组合收益 = 各股票区间收益的加权平均。
- 调仓时按【换手率 × 单边成本】扣交易成本。
- 这是"调仓日级"回测，不是日级逐笔，但对中低频轮动策略足够准确，且不会引入前视偏差。

注意：所有收益都用【后复权价】算，已含分红再投资逻辑。
"""
import numpy as np
import pandas as pd

import config


def run_backtest(price_panel: pd.DataFrame, weights: pd.DataFrame) -> pd.Series:
    """
    返回组合净值曲线（Series，index=调仓日, 起点=1.0）。
    """
    rebal_dates = weights.index.tolist()
    one_side_cost = config.COMMISSION + config.SLIPPAGE  # 买卖都收
    sell_extra = config.STAMP_TAX                         # 卖出额外印花税

    nav = [1.0]
    nav_dates = [rebal_dates[0]]
    prev_w = pd.Series(dtype=float)  # 上一期持仓权重（调仓后）

    for i in range(len(rebal_dates) - 1):
        d0, d1 = rebal_dates[i], rebal_dates[i + 1]
        target_w = weights.loc[d0]
        target_w = target_w[target_w > 0]

        # --- 调仓成本：基于换手率 ---
        # 把上期权重和目标权重对齐，逐票算买入/卖出额
        all_codes = prev_w.index.union(target_w.index)
        pw = prev_w.reindex(all_codes).fillna(0.0)
        tw = target_w.reindex(all_codes).fillna(0.0)
        diff = tw - pw
        buy_turnover = diff[diff > 0].sum()      # 总买入比例
        sell_turnover = -diff[diff < 0].sum()    # 总卖出比例
        cost = (buy_turnover + sell_turnover) * one_side_cost + sell_turnover * sell_extra

        # --- 持仓期收益 ---
        try:
            p0 = price_panel.loc[:d0, target_w.index].iloc[-1]
            p1 = price_panel.loc[:d1, target_w.index].iloc[-1]
        except (KeyError, IndexError):
            period_ret = 0.0
            drift_w = target_w
        else:
            stock_ret = (p1 / p0 - 1).fillna(0.0)
            period_ret = float((target_w * stock_ret).sum())
            # 期末权重漂移（用于下期换手率计算）
            drift_w = target_w * (1 + stock_ret)
            drift_w = drift_w / drift_w.sum() if drift_w.sum() > 0 else target_w

        net_ret = (1 + period_ret) * (1 - cost) - 1
        nav.append(nav[-1] * (1 + net_ret))
        nav_dates.append(d1)
        prev_w = drift_w

    return pd.Series(nav, index=pd.to_datetime(nav_dates), name="strategy")


def performance_metrics(nav: pd.Series, periods_per_year: int = 12) -> dict:
    """计算关键绩效指标。periods_per_year=12 因为是月度调仓。"""
    rets = nav.pct_change().dropna()
    total_return = nav.iloc[-1] / nav.iloc[0] - 1
    years = len(rets) / periods_per_year
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan

    vol = rets.std() * np.sqrt(periods_per_year)
    sharpe = (rets.mean() * periods_per_year) / vol if vol > 0 else np.nan

    # 最大回撤
    cummax = nav.cummax()
    drawdown = nav / cummax - 1
    max_dd = drawdown.min()

    win_rate = (rets > 0).mean()
    calmar = cagr / abs(max_dd) if max_dd != 0 else np.nan

    return {
        "总收益": f"{total_return:.1%}",
        "年化收益(CAGR)": f"{cagr:.1%}",
        "年化波动": f"{vol:.1%}",
        "夏普比率": f"{sharpe:.2f}",
        "最大回撤": f"{max_dd:.1%}",
        "卡玛比率": f"{calmar:.2f}",
        "月度胜率": f"{win_rate:.1%}",
    }
