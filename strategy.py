"""
策略层：月度动量轮动。

逻辑：
- 每月底，在当月沪深300成分股里，计算过去 N 个月的动量（后复权收益率）。
- 选动量最强的 Top N 只，下月等权持有。
- 下个调仓日重复。

动量（momentum）是学术界和业界最经得起检验的因子之一：
过去涨得好的，未来一段时间倾向继续偏强（行为金融学的"惯性"）。
"""
import pandas as pd

import config


def generate_target_weights(price_panel: pd.DataFrame,
                            members_by_month: dict,
                            month_ends: list,
                            lookback: int = None,
                            top_n: int = None) -> pd.DataFrame:
    """
    生成每个调仓日的目标权重表。
    返回 DataFrame：index=调仓日, columns=ts_code, values=权重(等权, 和=1)。
    lookback/top_n 不传时用 config 默认值（供 web 界面动态调参）。
    """
    lookback = config.MOMENTUM_LOOKBACK if lookback is None else int(lookback)
    top_n = config.HOLD_TOP_N if top_n is None else int(top_n)
    weights = {}

    for i, me in enumerate(month_ends):
        if i < lookback:
            continue  # 前 lookback 个月没有足够历史算动量
        me = pd.to_datetime(me)
        lookback_date = pd.to_datetime(month_ends[i - lookback])

        # 当月成分股
        pool = members_by_month.get(me) or members_by_month.get(month_ends[i])
        if not pool:
            continue
        pool = [c for c in pool if c in price_panel.columns]

        # 动量 = 区间收益率，要求两个端点都有价格（剔除停牌/新股）
        try:
            p_now = price_panel.loc[:me, pool].iloc[-1]
            p_then = price_panel.loc[:lookback_date, pool].iloc[-1]
        except (KeyError, IndexError):
            continue
        momentum = (p_now / p_then - 1).dropna()
        if momentum.empty:
            continue

        winners = momentum.sort_values(ascending=False).head(top_n).index
        w = pd.Series(1.0 / len(winners), index=winners)
        weights[me] = w

    wdf = pd.DataFrame(weights).T.fillna(0.0)
    wdf.index.name = "rebalance_date"
    return wdf


def _factor_asof(panel: pd.DataFrame, me, pool: list) -> pd.Series:
    """从因子面板取截至调仓日 me 的最新一行（时点对齐，避免未来数据）。"""
    if panel is None or panel.empty:
        return pd.Series(dtype=float)
    cols = [c for c in pool if c in panel.columns]
    if not cols:
        return pd.Series(dtype=float)
    try:
        return panel.loc[:me, cols].iloc[-1].dropna()
    except (KeyError, IndexError):
        return pd.Series(dtype=float)


def generate_factor_weights(price_panel: pd.DataFrame,
                            members_by_month: dict,
                            month_ends: list,
                            lookback: int = None,
                            top_n: int = None,
                            value_panel: pd.DataFrame = None,
                            value_weight: float = 0.0,
                            quality_panel: pd.DataFrame = None,
                            quality_weight: float = 0.0) -> pd.DataFrame:
    """
    通用多因子打分轮动。各因子按百分位排名（0=最差,1=最好）加权合成：
      综合分 = mom_w*动量分 + value_weight*估值分 + quality_weight*质量分
      mom_w = 1 - value_weight - quality_weight（下限 0）
    动量分：过去 lookback 月收益越高越好
    估值分：PB 越低越好（剔除 PB<=0）
    质量分：ROE 越高越好
    某因子数据不足时自动忽略其权重并归一化，退化到可用因子。
    """
    lookback = config.MOMENTUM_LOOKBACK if lookback is None else int(lookback)
    top_n = config.HOLD_TOP_N if top_n is None else int(top_n)
    vw = min(max(float(value_weight), 0.0), 1.0)
    qw = min(max(float(quality_weight), 0.0), 1.0)
    weights = {}

    for i, me in enumerate(month_ends):
        if i < lookback:
            continue
        me = pd.to_datetime(me)
        lookback_date = pd.to_datetime(month_ends[i - lookback])
        pool = members_by_month.get(me) or members_by_month.get(month_ends[i])
        if not pool:
            continue
        pool = [c for c in pool if c in price_panel.columns]

        try:
            p_now = price_panel.loc[:me, pool].iloc[-1]
            p_then = price_panel.loc[:lookback_date, pool].iloc[-1]
        except (KeyError, IndexError):
            continue
        momentum = (p_now / p_then - 1).dropna()
        if momentum.empty:
            continue

        # 各因子原始值（时点对齐）
        pb = _factor_asof(value_panel, me, pool)
        pb = pb[pb > 0]                       # 剔除负/零 PB
        roe = _factor_asof(quality_panel, me, pool)

        # 动态权重：数据不足的因子权重清零，再归一化
        w_mom = max(1.0 - vw - qw, 0.0)
        w_val = vw if len(pb) >= top_n else 0.0
        w_qual = qw if len(roe) >= top_n else 0.0
        total = w_mom + w_val + w_qual
        if total <= 0:
            w_mom, total = 1.0, 1.0
        w_mom, w_val, w_qual = w_mom / total, w_val / total, w_qual / total

        idx = momentum.index
        composite = w_mom * momentum.rank(pct=True)
        if w_val > 0:
            composite = composite.add(
                w_val * (-pb).rank(pct=True).reindex(idx).fillna(0.5), fill_value=0)
        if w_qual > 0:
            composite = composite.add(
                w_qual * roe.rank(pct=True).reindex(idx).fillna(0.5), fill_value=0)

        composite = composite.reindex(idx).dropna()
        if composite.empty:
            continue
        winners = composite.sort_values(ascending=False).head(top_n).index
        w = pd.Series(1.0 / len(winners), index=winners)
        weights[me] = w

    wdf = pd.DataFrame(weights).T.fillna(0.0)
    wdf.index.name = "rebalance_date"
    return wdf


def apply_timing_filter(weights: pd.DataFrame,
                        index_daily: pd.Series,
                        ma_days: int = None) -> pd.DataFrame:
    """
    风控择时叠加：每个调仓日看大盘是否在长期均线之上。
    跌破均线 → 该月清空持仓（持币），躲开熊市大跌。
    index_daily: 大盘指数日线序列（index=date, 收盘价，归一化与否不影响均线穿越判断）。
    """
    ma_days = config.TIMING_MA_DAYS if ma_days is None else int(ma_days)
    if weights.empty or index_daily is None or index_daily.empty:
        return weights
    idx = index_daily.sort_index()
    ma = idx.rolling(ma_days, min_periods=max(20, ma_days // 2)).mean()

    w = weights.copy()
    for d in list(w.index):
        dt = pd.to_datetime(d)
        price_sub = idx.loc[:dt]
        ma_sub = ma.loc[:dt]
        if price_sub.empty or ma_sub.empty or pd.isna(ma_sub.iloc[-1]):
            continue  # 均线数据不足 → 保持原仓（默认 risk-on）
        if price_sub.iloc[-1] < ma_sub.iloc[-1]:
            w.loc[d] = 0.0  # 跌破长期均线 → 空仓持币
    return w
