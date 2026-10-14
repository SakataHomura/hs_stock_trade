"""Fetch hy2 full ranking (count=124) and extract target sector codes as structured data."""
import asyncio
import sys
import json
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import getUrl, FinancePlugin

TARGETS = {
    "pt01801016": "种植业",
    "pt01801745": "军工电子Ⅱ",
    "pt01801093": "汽车零部件",
    "pt01801712": "玻璃玻纤",
    "pt01801036": "塑料",
    "pt01801072": "通用设备",
    "pt01801742": "航空装备Ⅱ",
    "pt01801051": "金属新材料",
    "pt01801017": "养殖业",
    "pt01801151": "化学制药",
    "pt01801152": "生物制品",
    "pt01801054": "小金属",
    "pt01801156": "医疗服务",
    "pt01801081": "半导体",
    "pt01801083": "元件",
    "pt01801078": "自动化设备",
    "pt01801055": "工业金属",
    "pt01801053": "贵金属",
}

async def main():
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy2&sort_type=PriceRatioD20&direct=down&selector=not_in_market%3Dgn_fg&count=124"
    response = await getUrl(url)
    data = json.loads(response)
    rank_list = data["data"].get("rank_list", [])
    total = data["data"].get("total", len(rank_list))
    # build code->rank index
    code_to_rank = {item.get("code",""): i+1 for i, item in enumerate(rank_list)}
    for item in rank_list[:2]:
        print("KEYS:", list(item.keys()))
        print("SAMPLE:", json.dumps(item, ensure_ascii=False)[:400])
    for item in rank_list:
        code = item.get("code","")
        if code in TARGETS:
            zgb = item.get("zgb","")  # e.g. "1/11"
            up_ratio = 0.0
            if "/" in str(zgb):
                try:
                    up, tot = str(zgb).split("/")
                    up_ratio = round(float(up)/float(tot)*100, 2) if float(tot)>0 else 0.0
                except: up_ratio = 0.0
            print(f"CODE={code} NAME={TARGETS[code]} RANK={code_to_rank[code]}/{total} "
                  f"PRICE_RATIO={item.get('zdf')} D5={item.get('zdf_d5')} "
                  f"D20={item.get('zdf_d20')} UP_RATIO={up_ratio} "
                  f"FLOW5={item.get('zljlr_d5')}")

if __name__ == "__main__":
    asyncio.run(main())
