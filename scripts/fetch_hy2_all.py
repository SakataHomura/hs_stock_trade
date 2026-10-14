"""Fetch hy2 full ranking (count=124) and grep specific sector codes."""
import asyncio
import sys
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import FinancePlugin

# target codes we care about
TARGETS = [
    "pt01801016",  # 种植业
    "pt01801745",  # 军工电子Ⅱ
    "pt01801093",  # 汽车零部件
    "pt01801712",  # 玻璃玻纤
    "pt01801036",  # 塑料
    "pt01801072",  # 通用设备
    "pt01801742",  # 航空装备Ⅱ
    "pt01801051",  # 金属新材料
    "pt01801017",  # 养殖业
    "pt01801151",  # 化学制药
    "pt01801152",  # 生物制品
    "pt01801054",  # 小金属
    "pt01801156",  # 医疗服务
    "pt01801081",  # 半导体
    "pt01801083",  # 元件
    "pt01801078",  # 自动化设备
    "pt01801055",  # 工业金属
    "pt01801053",  # 贵金属
]

async def main():
    b = FinancePlugin()
    ret = await b.querySectorRank(board_type="hy2", sort_type="PriceRatioD20")
    lines = ret.split("\n")
    for line in lines:
        for code in TARGETS:
            if code in line:
                print(line.strip())
                break

if __name__ == "__main__":
    asyncio.run(main())
