#!/usr/bin/env python3
"""获取hy2全量板块排行数据，提取追踪板块的涨股比/排名/涨幅/资金流"""
import sys, asyncio, json
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import getUrl

TRACK = {
    "pt01801733": "其他电源设备Ⅱ",
    "pt01801971": "环境治理",
    "pt01801737": "电池",
    "pt01801034": "化学制品",
    "pt01801738": "电网设备",
    "pt01801033": "化学原料",
    "pt01801074": "专用设备",
    "pt01801053": "贵金属",
    "pt01801962": "油服工程",
    "pt01801055": "工业金属",
    "pt01801020": "油服工程(旧)",
    "pt01801044": "电力(旧)",
    "pt01801106": "通信设备(旧)",
    "pt01801050": "工业金属(旧)",
    "pt01801052": "贵金属(旧)",
    "pt01801105": "半导体(旧)",
}

async def main():
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
           "board_type=hy2&sort_type=PriceRatioD20&direct=down"
           "&selector=not_in_market%3Dgn_fg&count=200")
    resp = await getUrl(url)
    data = json.loads(resp)
    rank_list = data["data"].get("rank_list", [])
    print(f"total sectors: {len(rank_list)}")
    for i, item in enumerate(rank_list, 1):
        code = item.get("code", "")
        if code in TRACK:
            zgb = item.get("zgb", "")
            # zgb 形如 "12/45"
            ratio = None
            try:
                up, tot = zgb.split("/")
                ratio = round(float(up) / float(tot) * 100, 1) if float(tot) > 0 else 0
            except Exception:
                pass
            print(json.dumps({
                "rank": i, "code": code, "name": item.get("name"),
                "zdf": item.get("zdf"), "zdf_d5": item.get("zdf_d5"),
                "zdf_d20": item.get("zdf_d20"), "zgb": zgb, "ratio": ratio,
                "zljlr_d5": item.get("zljlr_d5"),
            }, ensure_ascii=False))

asyncio.run(main())
