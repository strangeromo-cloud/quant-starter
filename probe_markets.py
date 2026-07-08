"""
探测 Tushare 港美股 / 全球指数 接口的可用性、数据格式、字段名。
用法（在服务器）：
    docker run --rm -e TUSHARE_TOKEN=你的token quant-bt python probe_markets.py

根据输出我才能精准实现港美股版（避免盲猜代码格式/字段/权限）。
"""
import os
import tushare as ts

ts.set_token(os.environ.get("TUSHARE_TOKEN", ""))
pro = ts.pro_api()


def probe(title, fn):
    print("\n=== " + title + " ===")
    try:
        df = fn()
        if df is None or len(df) == 0:
            print("  返回空（多半是无权限/积分不够，或该参数无数据）")
            return
        print("  ✅ OK  行数:", len(df))
        print("  列:", list(df.columns))
        print(df.head(3).to_string(max_colwidth=14))
    except Exception as e:
        print("  ❌ FAIL:", repr(e)[:200])


# ---- 全球指数（基准 + 风控择时用）----
probe("index_global SPX 标普500",
      lambda: pro.index_global(ts_code="SPX", start_date="20240102", end_date="20240131"))
probe("index_global IXIC 纳斯达克",
      lambda: pro.index_global(ts_code="IXIC", start_date="20240102", end_date="20240131"))
probe("index_global HSI 恒生指数",
      lambda: pro.index_global(ts_code="HSI", start_date="20240102", end_date="20240131"))

# ---- 美股 ----
probe("us_basic 美股列表", lambda: pro.us_basic(offset=0, limit=5))
probe("us_daily AAPL.O", lambda: pro.us_daily(ts_code="AAPL.O", start_date="20240102", end_date="20240131"))
probe("us_daily AAPL", lambda: pro.us_daily(ts_code="AAPL", start_date="20240102", end_date="20240131"))
# 美股复权因子（若有）
probe("us_daily_adj/复权 探测", lambda: pro.us_daily(ts_code="AAPL.O", start_date="20240102", end_date="20240105"))

# ---- 港股 ----
probe("hk_basic 港股列表", lambda: pro.hk_basic(limit=5))
probe("hk_daily 腾讯 00700.HK",
      lambda: pro.hk_daily(ts_code="00700.HK", start_date="20240102", end_date="20240131"))

print("\n探测完成。把以上完整输出贴回来即可。")
