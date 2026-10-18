"""
短线观察雷达卡片（Lark）：交易环境（视频"三看"）→ 今日形态 → 纸面跟踪 → 回测结论。

⚠️ 雷达模式（2026-10 回测结论）：Martin 式规则的日线近似在 2012-2026、300 只美股上期望 -0.31R/笔、
年化 -5.5%，远不及同池等权 +16.7%，两段样本外都为负（见 docs/SWING_RESEARCH.md）。所以卡片只列
"系统看到了什么"并做纸面跟踪，不给建议股数、不发买入指令；纸面满 100 笔且期望 R 显著 >0 前不讨论实盘。

为什么单独一张卡、单独一个时间：持仓买卖点卡（notify_lark.py）是早上 8:00 看"拿着的怎么办"；
这张是北京时间 20:30（美股开盘前）看"按视频方法，系统今晚看到了什么"。两件事节奏不同，
混在一张卡里会互相淹没。任何环节失败都降级：环境失败不显示环境块；扫描整体失败发一张
"今日扫描失败"简卡；绝不整体崩。

用法：
    python notify_swing.py                 # 扫描 + 写库 + 发 Lark（需 DB / TWELVEDATA_KEY / LARK_WEBHOOK）
    python notify_swing.py --dry-run --limit 12 --out /path/card.json
                                           # 不写库不发送，卡片 JSON 写文件并打印概要
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

import config
import swing_scan

HEADER_COLOR = {"go": "green", "selective": "orange", "stop": "red"}
SOURCE_CN = {"gainer": "新进", "carry": "留观", "rs": "强势补充"}
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "swing_card_preview.json")


def _c(key, default=None):
    return config.SWING.get(key, default)


def _div(md: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": md}}


def _arrow(up) -> str:
    return "↑" if up else "↓"


def _idx_line(name: str, s: dict) -> str:
    p9 = "站上" if s.get("above9") else "跌破"
    p21 = "站上" if s.get("above21") else "跌破"
    ext = s.get("ext_pct")
    ext_s = f" 距21EMA {ext:+.1%}" if ext is not None else ""
    days = s.get("days_above21") or 0
    days_s = f"（连续{days}天在上）" if days else ""
    return (f"{name} {s.get('close')}：{p9}9EMA{_arrow(s.get('rising9'))} · "
            f"{p21}21EMA{_arrow(s.get('rising21'))}{ext_s}{days_s}")


def env_block(env: dict) -> dict:
    """交易环境块：结论 + 逐条理由 + QQQ/IWM + 宽度 + 自己的连亏。"""
    lines = [f"🌡️ **交易环境（视频'三看'）：{env['emoji']} {env['label']}**"]
    lines += [f"· {r}" for r in env.get("reasons") or []]
    grey = [_idx_line(n, s) for n, s in (env.get("index") or {}).items()]
    b = env.get("breadth")
    if b:
        prev = b.get("strong_pct_prev")
        prev_s = f"，5日前 {prev:.0%}" if prev is not None else ""
        grey.append(f"宽度：强 {b['strong']} · 中 {b['medium']} · 弱 {b['weak']}"
                    f"（强档 {b['strong_pct']:.0%}{prev_s}）· {b.get('source', '')}")
    own = env.get("own")
    if own:
        rm = own.get("risk_mult")
        rm_s = f" · 风险系数×{rm:g}" if isinstance(rm, (int, float)) else ""
        pause = " · 暂停开新仓" if own.get("pause") else ""
        grey.append(f"自己的单子：连亏 {int(own.get('consecutive_losses') or 0)} 笔{rm_s}{pause}"
                    + (f" · {own['note']}" if own.get("note") else ""))
    if grey:
        lines.append("<font color='grey'>" + "\n".join(grey) + "</font>")
    return _div("\n".join(lines))


def _setup_lines(i: int, s: dict) -> str:
    sp = s.get("stop_pct")
    sp_s = f"(-{sp:.1%})" if sp is not None else ""
    adr = s.get("adr_pct")
    adr_s = f" · ADR {adr:.1%}" if adr is not None else ""
    head = (f"**{i}. {s['name']} {s['ticker']}** · {s['setup_cn']} · 触发 >{s['trigger']:.2f}"
            f" · 止损 {s['stop']:.2f}{sp_s}{adr_s}")
    size = f"质量 {s['quality']:.0f}"     # 雷达模式：不给建议股数（回测无优势，见模块说明）
    tier = swing_scan.TIER_CN.get(s.get("tier"), "")
    grey = f"{tier}档 · " if tier else ""
    return f"{head}\n{size}\n<font color='grey'>{grey}{s.get('note') or ''}</font>"


def setups_block(res: dict, env: dict = None) -> dict:
    setups = res.get("setups") or []
    ok = [s for s in setups if s.get("ok_stop")]
    ok.sort(key=lambda s: -(s.get("quality") or 0))
    wide = len(setups) - len(ok)
    nmax = int(_c("card_max_setups", 8))
    lines = [f"📡 **今日形态**（止损合格 {len(ok)} 个 · 仅观察，不是买入信号）"]
    if env and env.get("verdict") == "stop":
        lines.append("<font color='grey'>按视频标准，这种环境不开新仓</font>")
    elif env and env.get("verdict") == "selective":
        lines.append("<font color='grey'>按视频标准，这种环境只看最前面一两只</font>")
    if ok:
        lines += [_setup_lines(i, s) for i, s in enumerate(ok[:nmax], 1)]
        if len(ok) > nmax:
            lines.append(f"<font color='grey'>另有 {len(ok) - nmax} 个合格形态未列出</font>")
    else:
        lines.append("今天没有止损合格的形态。")
    if wide:
        lines.append(f"<font color='grey'>另有 {wide} 个形态止损过宽（> min(ADR/2, 3%)），放观察等收窄</font>")
    wl = res.get("watchlist") or []
    if wl:
        cnt = {}
        for w in wl:
            cnt[w.get("source")] = cnt.get(w.get("source"), 0) + 1
        parts = " · ".join(f"{SOURCE_CN.get(k, k)} {v}" for k, v in cnt.items())
        top = "、".join(f"{w['ticker']}" for w in wl[:8])
        lines.append(f"<font color='grey'>观察名单 {len(wl)} 只（{parts}）：{top}"
                     f"{' …' if len(wl) > 8 else ''}</font>")
    return _div("\n\n".join(lines))


def paper_line(paper: dict) -> dict:
    if not paper or (not paper.get("n_closed") and not paper.get("open")):
        return _div("📒 **纸面跟踪**：刚开始（信号按规则自动推进，用 R 记账）\n"
                    "<font color='grey'>满 100 笔且期望 R 的 95% 区间下沿 >0 之前，不讨论实盘</font>")
    n = paper.get("n_closed") or 0
    opn = len(paper.get("open") or [])
    if n:
        aw, al = paper.get("avg_win_r"), paper.get("avg_loss_r")
        aw_s = f"{aw:+.2f}R" if aw is not None else "—"
        al_s = f"{al:+.2f}R" if al is not None else "—"
        body = (f"已平 {n} 笔 · 胜率 {paper['win_rate']:.0%} · 平均赚 {aw_s} / 亏 {al_s} · "
                f"期望 {paper['expectancy_r']:+.2f}R · 累计 {paper['total_r']:+.1f}R")
    else:
        body = "还没有平仓的笔"
    return _div(f"📒 **纸面跟踪**：{body} · 持仓/待触发 {opn} 笔\n"
                f"<font color='grey'>满 100 笔且期望 R 的 95% 区间下沿 >0 之前，不讨论实盘</font>")


def _note(extra: str = "") -> dict:
    txt = (f"雷达模式：{_c('backtest_note', '回测进行中')} · 只作观察与纸面跟踪，不是买入信号 · "
           "若自行交易：先定止损、单笔风险≤0.5% · 非投资建议")
    if extra:
        txt = extra + " · " + txt
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": txt}]}


def build_card(res: dict) -> dict:
    """扫描结果 → Lark 卡片。各块独立降级：某块构建失败就跳过该块。"""
    env = res.get("env")
    asof = res.get("asof") or ""
    elements = []
    if env:
        try:
            elements += [env_block(env), {"tag": "hr"}]
        except Exception as e:
            print("[notify_swing] 环境块失败（跳过）:", repr(e)[:100])
            env = None
    if not env:      # 没有环境结论 ≠ 环境好：明说，免得默认当成"可以放手做"
        elements += [_div("🌡️ **环境未评估**（指数数据缺失）<font color='grey'>：环境结论今天不可用</font>"), {"tag": "hr"}]
    try:
        elements.append(setups_block(res, env))
    except Exception as e:
        print("[notify_swing] 形态块失败:", repr(e)[:100])
        elements.append(_div("⚡ **今日形态**：生成失败，见日志"))
    elements.append({"tag": "hr"})
    try:
        elements.append(paper_line(res.get("paper")))
    except Exception as e:
        print("[notify_swing] 纸面块失败（跳过）:", repr(e)[:100])
    extra = ""
    if res.get("errors"):
        extra = f"部分取数失败 {len(res['errors'])} 处"
    elements.append(_note(extra))
    tmpl = HEADER_COLOR.get((env or {}).get("verdict"), "blue")
    return {"config": {"wide_screen_mode": True},
            "header": {"template": tmpl,
                       "title": {"tag": "plain_text", "content": f"📡 短线观察雷达 · {asof}"}},
            "elements": elements}


def build_fail_card(err: str, asof: str = "") -> dict:
    title = "📡 短线观察雷达" + (f" · {asof}" if asof else "")
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
            "elements": [_div(f"⚠️ **今日扫描失败**\n<font color='grey'>{str(err)[:300]}</font>\n"
                              "雷达今日无数据，明天自动重试。"),
                         _note()]}


def summarize(res: dict) -> str:
    env = res.get("env") or {}
    ok = [s for s in res.get("setups") or [] if s.get("ok_stop")]
    return (f"asof={res.get('asof')} 环境={env.get('verdict', '无')} 名单={len(res.get('watchlist') or [])} "
            f"形态={len(res.get('setups') or [])}(合格{len(ok)}) 初筛池={res.get('universe_size')} "
            f"TwelveData={res.get('credits_used')}次 错误={len(res.get('errors') or [])}")


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description="美股开盘前作业 Lark 卡片")
    ap.add_argument("--dry-run", action="store_true", help="不写库、不发送，卡片 JSON 写到 --out")
    ap.add_argument("--limit", type=int, default=None, help="最多取多少只个股行情（省额度）")
    ap.add_argument("--out", default=os.environ.get("SWING_CARD_PREVIEW", DEFAULT_OUT),
                    help="dry-run 时卡片 JSON 写到哪")
    a = ap.parse_args(argv)
    res = None
    try:
        res = swing_scan.run(dry_run=a.dry_run, limit=a.limit)
        card = build_card(res)
    except Exception as e:
        traceback.print_exc()
        card = build_fail_card(repr(e)[:200], (res or {}).get("asof", ""))
    if a.dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"card": card, "result": res}, f, ensure_ascii=False, indent=2, default=str)
        print(f"[notify_swing] dry-run：卡片已写 {a.out}")
        if res:
            print("[notify_swing]", summarize(res))
        for el in card["elements"]:
            if el.get("tag") == "div":
                print(el["text"]["content"])
                print("-" * 40)
        return {"card": card, "result": res}
    try:
        import notify_lark
        print("[notify_swing] Lark 响应:", notify_lark.send_lark_card(card))
    except Exception as e:
        print("[notify_swing] 发送失败:", repr(e)[:160])
    return {"card": card, "result": res}


if __name__ == "__main__":
    main(sys.argv[1:])
