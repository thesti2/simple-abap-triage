"""Read-only check: can Python reach the local Eclipse ADT MCP server? Lists tools, logged-on destinations,
and reads one line of ZTRIAGE_DEMO_BUG. Changes nothing in SAP.

Run: uv run python scripts/smoke_adt_mcp.py
"""

import asyncio
import os

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()


async def main() -> None:
    client = MultiServerMCPClient({
        "sap-adt": {
            "transport": "streamable_http",
            "url": os.environ["SAP_ADT_MCP_URL"],
            "headers": {"Authorization": os.environ["SAP_ADT_MCP_AUTH"]},
        }
    })
    tools = {t.name: t for t in await client.get_tools()}
    print(f"{len(tools)} tools:", ", ".join(sorted(tools)))
    dest = await tools["abap_destination_list"].ainvoke({})
    print("destinations:", "(1 logged on)" if dest else "none")
    destination = os.environ.get("SAP_ADT_DESTINATION", "")
    src = await tools["abap_object_read_file"].ainvoke({
        "destination": destination,
        "objectIdentifier": {"objectName": "ZTRIAGE_DEMO_BUG", "objectType": "PROG"},
        "offset": 55, "limit": 1,
    })
    print("read ZTRIAGE_DEMO_BUG:", "ok" if "p_qty" in str(src).lower() else f"unexpected: {str(src)[:200]}")
    for name in ("abap_transport_get_unified_diff", "abap_unit_run_tests", "abap_object_edit_file"):
        print(f"\n{name} args:", list((tools[name].args or {}).keys()))


if __name__ == "__main__":
    asyncio.run(main())
