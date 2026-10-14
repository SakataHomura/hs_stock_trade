import sys, asyncio
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import FinancePlugin

async def main():
    b = FinancePlugin()
    data_type = sys.argv[1]
    code = sys.argv[2] if len(sys.argv) > 2 else ""
    kwargs = {}
    if len(sys.argv) > 3:
        # remaining args as key=value
        for a in sys.argv[3:]:
            if "=" in a:
                k, v = a.split("=", 1)
                kwargs[k] = v
    if data_type == "sector_rank":
        ret = await b.execute("sector_rank", board_type=kwargs.get("board_type","hy2"), sort_type=kwargs.get("sort_type","PriceRatioD20"), count=int(kwargs.get("count","124")))
    elif data_type == "constituent":
        ret = await b.execute("constituent", code=code)
    elif data_type == "security":
        ret = await b.execute("security", code=code)
    elif data_type == "quotes":
        ret = await b.execute("quotes", code=code, ktype=kwargs.get("ktype","day"))
    elif data_type == "fund_flow":
        ret = await b.execute("fund_flow", code=code, fund_flow_type=kwargs.get("fund_flow_type","both"), fund_flow_days=kwargs.get("fund_flow_days","20"))
    else:
        ret = await b.execute(data_type, code=code, **kwargs)
    print(ret)

asyncio.run(main())
