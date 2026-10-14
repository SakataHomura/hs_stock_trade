import sys, asyncio, json, urllib.request, urllib.parse

async def getUrl(url, timeout=15, max_retries=2):
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = urllib.request.urlopen(url, timeout=timeout)
            ret = response.read()
            try:
                return ret.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return ret.decode('gbk')
                except UnicodeDecodeError:
                    return ret.decode('latin1')
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                import asyncio as a
                await a.sleep(attempt + 1)
    raise Exception(f"请求失败: {url}, {last_error}")

async def main():
    # Fetch hy2 with UpStockRatio sort, count=60, to get trend type / stage / up-stock ratio for all sectors
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy2&sort_type=UpStockRatio&direct=down&selector=not_in_market%3Dgn_fg&count=60"
    resp = await getUrl(url)
    data = json.loads(resp)
    rank_list = data["data"].get("rank_list", [])
    out = {}
    for item in rank_list:
        code = item.get("code", "")
        out[code] = {
            "name": item.get("name", ""),
            "zdf": item.get("zdf", 0),
            "zdf_d5": item.get("zdf_d5", 0),
            "zdf_d20": item.get("zdf_d20", 0),
            "zgb": item.get("zgb", ""),  # 涨股比
            "zljlr_d5": item.get("zljlr_d5", 0),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))

asyncio.run(main())
