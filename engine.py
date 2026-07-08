"""
回测引擎封装：给一组参数 → 纯读库跑回测 → 返回 nav + 指标 + 基准。
web 界面和 CLI 都用这个，保证逻辑一致。不碰 Tushare（数据须已同步入库）。
"""
import pandas as pd

import config
import data
import strategy
import backtest


def run_backtest_job(params: dict) -> dict:
    """
    params: {strategy, index_code, lookback, top_n, start, end}
    返回: {nav:{dates,values}, benchmark:{dates,values}, metrics, benchmark_metrics, params}
    """
    strategy_type = params.get("strategy", "momentum")
    market = params.get("market", "A")
    use_timing = "timing" in strategy_type
    # 港美股无估值/质量数据，强制只用动量
    use_value = "multifactor" in strategy_type and market == "A"
    use_quality = "quality" in strategy_type and market == "A"
    index_code = params.get("index_code", config.INDEX_CODE)
    lookback = int(params.get("lookback", config.MOMENTUM_LOOKBACK))
    top_n = int(params.get("top_n", config.HOLD_TOP_N))
    timing_ma = int(params.get("timing_ma", config.TIMING_MA_DAYS))
    value_weight = float(params.get("value_weight", 0.5))
    quality_weight = float(params.get("quality_weight", 0.5))
    start = params.get("start", config.START_DATE)
    end = params.get("end", config.END_DATE)
    start_dt, end_dt = pd.to_datetime(start), pd.to_datetime(end)

    if market == "A":
        bench_code = index_code
        panel = data.load_price_panel()
        if panel.empty:
            raise RuntimeError("daily_price 表为空，请先同步数据（跑一次 run.py / deploy.sh）")
        panel = panel.loc[panel.index <= end_dt]
        month_ends = [m for m in data.month_ends_from_db() if pd.to_datetime(m) <= end_dt]
        members = data.load_members_from_db(index_code, month_ends)
        if not members:
            raise RuntimeError(f"index_member 表无 {index_code} 成分数据，请先同步")
    else:  # US / HK：静态成分池 + 全球指数基准
        if market not in config.MARKET_UNIVERSE:
            raise RuntimeError(f"不支持的市场: {market}")
        bench_code = config.MARKET_BENCHMARK[market]
        universe = config.MARKET_UNIVERSE[market]
        panel = data.load_price_panel(universe)
        if panel.empty:
            raise RuntimeError(f"{market} 行情为空，请先同步港美股数据（跑 sync_global.py）")
        panel = panel.loc[panel.index <= end_dt]
        month_ends = [m for m in data.month_ends_for_codes(universe) if pd.to_datetime(m) <= end_dt]
        members = {pd.to_datetime(me): list(universe) for me in month_ends}

    if use_value or use_quality:
        value_panel = None
        quality_panel = None
        if use_value:
            value_panel = data.load_pb_panel()
            if value_panel.empty:
                raise RuntimeError("估值数据(daily_basic)为空，请先同步：TUSHARE_TOKEN=xxx ./deploy.sh")
        if use_quality:
            quality_panel = data.load_roe_asof_panel(month_ends)
            if quality_panel.empty:
                raise RuntimeError("质量数据(fina_indicator ROE)为空，请先同步：TUSHARE_TOKEN=xxx ./deploy.sh")
        weights = strategy.generate_factor_weights(
            panel, members, month_ends, lookback=lookback, top_n=top_n,
            value_panel=value_panel, value_weight=value_weight if use_value else 0.0,
            quality_panel=quality_panel, quality_weight=quality_weight if use_quality else 0.0)
    else:
        weights = strategy.generate_target_weights(panel, members, month_ends,
                                                   lookback=lookback, top_n=top_n)
    if weights.empty:
        raise RuntimeError("回测区间太短或无足够历史算因子，请放宽区间/减小 lookback")

    # 风控择时叠加（在全历史权重上做，时点对齐只用 <= 调仓日的数据）
    if use_timing:
        idx_daily = data.load_benchmark_from_db(bench_code)
        if idx_daily.empty:
            idx_daily = data.load_benchmark_from_db(config.BENCHMARK_CODE)
        weights = strategy.apply_timing_filter(weights, idx_daily, ma_days=timing_ma)

    # 只保留测试窗口 [start, end] 内的调仓（窗口前仅用于热身/算动量）
    weights = weights.loc[weights.index >= start_dt]
    if weights.empty:
        raise RuntimeError("测试窗口内无调仓，请放宽区间")

    nav = backtest.run_backtest(panel, weights)
    metrics = backtest.performance_metrics(nav)

    bench = data.load_benchmark_from_db(bench_code)
    if not bench.empty:
        bench = bench.reindex(nav.index, method="ffill")
        bench = bench / bench.iloc[0]
        bench_metrics = backtest.performance_metrics(bench)
        bench_out = _series_to_json(bench)
    else:
        bench_metrics, bench_out = {}, {"dates": [], "values": []}

    return {
        "nav": _series_to_json(nav),
        "benchmark": bench_out,
        "metrics": metrics,
        "benchmark_metrics": bench_metrics,
        "params": {"strategy": strategy_type, "market": market,
                   "index_code": index_code, "lookback": lookback,
                   "top_n": top_n, "timing_ma": timing_ma,
                   "value_weight": value_weight, "quality_weight": quality_weight,
                   "start": start, "end": end},
    }


def _series_to_json(s: pd.Series) -> dict:
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in s.index],
        "values": [round(float(v), 4) for v in s.values],
    }
