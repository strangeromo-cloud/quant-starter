"""
每日持仓择时信号推送到 Lark。流程：刷新行情 → 算信号 → 带签名发卡片。
用法（服务器，需 ~/quant.env 提供 DB / TWELVEDATA_KEY / LARK_WEBHOOK / LARK_SECRET）：
    docker run --rm --network app-net --env-file ~/quant.env quant-bt python notify_lark.py
"""
import os
import time
import json
import hmac
import base64
import hashlib
import urllib.request

import config
import data
import signals
import levels
import macro
import sector_rotation


def _gen_sign(ts: str, secret: str) -> str:
    raw = f"{ts}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(raw, digestmod=hashlib.sha256).digest()).decode()


def send_lark_card(card: dict) -> str:
    url = os.environ["LARK_WEBHOOK"]
    secret = os.environ.get("LARK_SECRET", "")
    body = {"msg_type": "interactive", "card": card}
    if secret:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = _gen_sign(ts, secret)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=20).read().decode()


_TRIGGER_META = {  # 触发类型 → (emoji, 中文, 取哪个价位字段)
    "buy":         ("🟢", "买入", "buy"),
    "take_profit": ("🎯", "止盈", "take_profit"),
    "stop_loss":   ("🔴", "止损", "stop_loss"),
}


def _fmt_full_line(lv: dict, sig_action: str) -> str:
    """每日全量列表里每只两行：数值行 + 灰字理由行。"""
    if lv["status"] != "ok":
        return f"**{lv['name']}**　<font color='grey'>{lv.get('action', '无数据')}</font>"
    tp = lv["take_profit"] if lv["take_profit"] is not None else "—"
    if lv["stop_loss"] is None:  # 空仓
        sl_seg = "损 —"
    else:
        sl_tag = "损" if lv["sl_kind"] == "止损" else "移损"
        sl_seg = f"{sl_tag} {lv['stop_loss']}"
    body = f"**{lv['name']}**　现 {lv['price']}｜买 {lv['buy']}｜盈 {tp}｜{sl_seg}"
    tail = f"距买 {lv['to_buy']}"
    if sig_action:
        tail += f" · {sig_action}"
    reason = f"买点：{lv['buy_reason']}\n卖点：{lv['sell_reason']}"
    return f"{body}　<font color='grey'>({tail})</font>\n<font color='grey'>{reason}</font>"


def _macro_banner(mk: dict) -> dict:
    """市场风险环境横幅（卡片顶部）。mk 为 None 时调用方跳过。"""
    if mk.get("source"):  # 真实 FRED HY OAS
        detail = f"{mk['source']} · {mk['credit_trend']} · {mk['note']}"
    else:                 # Twelve Data ETF 代理
        if mk.get("spy_above_ma") is True:
            spy_txt = "标普站上年线"
        elif mk.get("spy_above_ma") is False:
            spy_txt = "标普跌破年线"
        else:
            spy_txt = "标普年线数据不足"
        detail = f"信用利差代理(HYG/LQD) {mk['credit_trend']} · {spy_txt} · {mk['note']}"
    content = (f"🌡️ **市场风险环境**：{mk['emoji']} {mk['status']}（风险分 {mk['score']}/100）\n"
               f"<font color='grey'>{detail}</font>")
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _sector_banner(sec: dict) -> dict:
    """板块强弱榜（动量集成排名，回测验证的轮动信号）。sec 为 None 时调用方跳过。"""
    medals = ["🥇", "🥈", "🥉"]
    top3 = "｜".join(f"{medals[i]} {sym} {m:+.0%}" for i, (sym, m) in enumerate(sec["ranking"][:3]))
    mine = []
    for sym, (rank, m) in sorted(sec["holding_ranks"].items(), key=lambda x: x[1][0]):
        warn = " ⚠️" if rank > sec["total"] * 2 / 3 else ""
        mine.append(f"{sym}({sector_rotation.HOLDING_SECTORS[sym]}) #{rank}{warn}")
    content = (f"📊 **板块强弱榜**（3/6/12月动量集成）\n{top3}\n"
               f"<font color='grey'>你的板块：{' · '.join(mine)}（共{sec['total']}个）</font>")
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def build_card(lvls: list, sigs: list, asof: str, mk: dict = None, sec: dict = None) -> dict:
    sig_by_ticker = {s["ticker"]: s for s in sigs}

    # 触发区（置顶高亮，带理由）
    hit_blocks = []
    for lv in lvls:
        for kind in lv.get("triggers", []):
            emoji, label, field = _TRIGGER_META[kind]
            op = "≤" if kind != "take_profit" else "≥"
            reason = lv["buy_reason"] if kind == "buy" else lv["sell_reason"]
            hit_blocks.append(
                f"{emoji} **{lv['name']} · {label}触发**\n"
                f"现价 {lv['price']} {op} {label}点 {lv[field]}\n"
                f"<font color='grey'>理由：{reason}</font>")

    # 全量区
    full_lines = [_fmt_full_line(lv, sig_by_ticker.get(lv["ticker"], {}).get("action", ""))
                  for lv in lvls]

    header_tmpl = "red" if hit_blocks else "blue"
    elements = []
    if mk:  # 市场风险环境横幅置顶
        elements.append(_macro_banner(mk))
        elements.append({"tag": "hr"})
    if sec:  # 板块强弱榜
        elements.append(_sector_banner(sec))
        elements.append({"tag": "hr"})
    if hit_blocks:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "🔔 **达到点位提醒**\n\n" + "\n\n".join(hit_blocks)}})
        elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md",
                     "content": "📋 **持仓买卖点**\n\n" + "\n\n".join(full_lines)}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                     "content": "每日自动推送 · 买=近高回撤低吸 盈/损=止盈止损 · 仅技术信号，非投资建议"}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_tmpl,
                   "title": {"tag": "plain_text", "content": f"⛨ 持仓买卖点 · {asof}"}},
        "elements": elements,
    }


def main():
    tickers = config.PORTFOLIO_DEFAULT
    try:
        data.refresh_holding_prices(tickers)  # 推送前先刷到最新
    except Exception as e:
        print("[notify] 刷新行情失败（用已有数据继续）:", repr(e)[:120])
    lvls = levels.compute(tickers)
    sigs = signals.compute(tickers, ma_days=200)
    mk = macro.assess_market_risk()  # 市场风险环境（取数失败返回 None，不影响推送）
    sec = sector_rotation.compute()  # 板块强弱榜（失败返回 None，不影响推送）
    asof = (next((lv.get("asof") for lv in lvls if lv.get("asof")), "")
            or next((s.get("asof") for s in sigs if s.get("asof")), ""))
    resp = send_lark_card(build_card(lvls, sigs, asof, mk, sec))
    print("[notify] Lark 响应:", resp)


if __name__ == "__main__":
    main()
