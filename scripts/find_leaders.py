"""Find Tier1/Tier2 leaders in 通用设备 pt01801072 by market cap + RPS_20."""
import asyncio, sys, json
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import FinancePlugin

async def main():
    b = FinancePlugin()
    # get all constituents (up to 200)
    ret = await b.queryConstituent('pt01801072')
    # parse the markdown table
    lines = ret.split("\n")
    rows = []
    for line in lines:
        if line.startswith("|") and "股票代码" not in line and "---" not in line:
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) >= 14 and cols[0].startswith("sz") or cols[0].startswith("sh"):
                try:
                    code = cols[0]
                    name = cols[1]
                    rps = float(cols[2])
                    price = float(cols[9])
                    mcap = float(cols[13])  # 流通市值(亿元)
                    rows.append({"code":code,"name":name,"rps":rps,"mcap":mcap})
                except Exception as e:
                    pass
    # sort by mcap desc
    rows.sort(key=lambda x: x["mcap"], reverse=True)
    print("=== Top by market cap ===")
    for r in rows[:25]:
        tier = ""
        if r["mcap"] >= 500 and r["rps"] > 80: tier = "Tier1"
        elif r["mcap"] >= 300 and r["rps"] > 70: tier = "Tier2"
        print(f"{r['code']} {r['name']:<8} mcap={r['mcap']:.1f}亿 rps={r['rps']:.1f} {tier}")

if __name__ == "__main__":
    asyncio.run(main())
