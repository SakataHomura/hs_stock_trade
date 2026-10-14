"""Fetch hy2 sector ranking and output as JSON-friendly text for sector tracker."""
import asyncio
import sys
import os
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import FinancePlugin

async def main():
    b = FinancePlugin()
    ret = await b.querySectorRank(board_type="hy2", sort_type="PriceRatioD20")
    print(ret)

if __name__ == "__main__":
    asyncio.run(main())
