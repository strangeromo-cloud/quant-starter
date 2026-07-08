"""
主入口：跑通完整闭环（数据存服务器 MySQL quant 库）。
    建表 → 同步成分股 → 同步价格 → 信号 → 回测 → 评估 → 可视化

用法（在服务器 docker 容器内）：
    python run.py
环境变量：TUSHARE_TOKEN, DB_HOST=mysql, DB_USER, DB_PASSWORD, DB_NAME=quant
"""
import os
import matplotlib
import matplotlib.pyplot as plt

import config
import db
import data
import strategy
import backtest

matplotlib.use("Agg")  # 服务器无显示环境
matplotlib.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False


def main():
    print("=" * 50)
    print("月度动量轮动策略回测（数据落 MySQL quant 库）")
    print(f"区间 {config.START_DATE} ~ {config.END_DATE} | 股票池 {config.INDEX_CODE} | "
          f"动量{config.MOMENTUM_LOOKBACK}月 | Top{config.HOLD_TOP_N}")
    print("=" * 50)

    db.init_tables()
    print("[0/5] 数据表就绪")

    month_ends = data.get_month_end_dates(config.START_DATE, config.END_DATE)
    print(f"[1/5] 调仓日 {len(month_ends)} 个")

    members = data.sync_index_members(config.INDEX_CODE, month_ends)
    all_codes = sorted({c for codes in members.values() for c in codes})
    print(f"[2/5] 涉及股票 {len(all_codes)} 只（历史成分并集）")

    data.sync_prices(all_codes, config.START_DATE, config.END_DATE)
    data.sync_daily_basic(month_ends)  # 估值数据（PB，多因子用）
    data.sync_fina_indicator(all_codes)  # 财务数据（ROE，质量因子用）
    panel = data.load_price_panel()
    print(f"[3/5] 价格面板 {panel.shape[0]} 日 × {panel.shape[1]} 股")

    weights = strategy.generate_target_weights(panel, members, month_ends)
    nav = backtest.run_backtest(panel, weights)
    print(f"[4/5] 回测完成，{len(nav)} 个调仓期")

    metrics = backtest.performance_metrics(nav)
    bench = data.sync_benchmark(config.BENCHMARK_CODE, config.START_DATE, config.END_DATE)
    bench = bench.reindex(nav.index, method="ffill")
    bench = bench / bench.iloc[0]
    bench_metrics = backtest.performance_metrics(bench)

    print("\n" + "=" * 50)
    print("绩效报告".center(40))
    print("=" * 50)
    print(f"{'指标':<16}{'策略':>12}{'沪深300':>12}")
    print("-" * 50)
    for k in metrics:
        print(f"{k:<16}{metrics[k]:>12}{bench_metrics.get(k, '-'):>12}")
    print("=" * 50)

    plt.figure(figsize=(11, 5))
    plt.plot(nav.index, nav.values, label="动量轮动策略", linewidth=2)
    plt.plot(bench.index, bench.values, label="沪深300基准", linewidth=1.5, alpha=0.7)
    plt.title("月度动量轮动 vs 沪深300")
    plt.ylabel("净值（起点=1）")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    out = "output/result_nav.png" if os.path.isdir("output") else "result_nav.png"
    plt.savefig(out, dpi=130)
    print(f"\n净值曲线已保存：{out}")


if __name__ == "__main__":
    main()
