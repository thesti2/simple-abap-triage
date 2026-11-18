"""TR release through ABAP Unit (helper class ZCL_TRIAGE_EVIDENCE -> RFC module Z_TRIAGE_TR_RELEASE)."""

from __future__ import annotations

from typing import Any

from app.fix.adt import AdtGateway
from app.fix.evidence import EVIDENCE_CLASS, TEST_INCLUDE, parse_release_output, render_release_include


async def prepare_include(adt: AdtGateway, content: str) -> None:
    current = await adt.read_file(TEST_INCLUDE)
    if current != content:
        res = await adt.write_file(TEST_INCLUDE, current, content)
        if "successful" not in res.lower():
            raise RuntimeError(f"cannot prepare helper test include: {res[:200]}")
        act = await adt.activate(EVIDENCE_CLASS, "CLAS")
        if "successful" not in act.lower():
            raise RuntimeError(f"cannot activate {EVIDENCE_CLASS}: {act[:300]}")


async def release_transport(adt: AdtGateway, transport: str) -> dict[str, Any]:
    """Release a TR (tasks first, then request). Returns {"released": bool, "status": str, "messages": [...]}."""
    await prepare_include(adt, render_release_include(transport))
    result = parse_release_output(await adt.run_unit_tests(EVIDENCE_CLASS))
    result["released"] = result.get("released") in ("X", True)
    return result


if __name__ == "__main__":  # uv run python -m app.fix.release <TR>
    import asyncio
    import os
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    gw = AdtGateway(os.environ["SAP_ADT_MCP_URL"], os.environ["SAP_ADT_MCP_AUTH"], os.environ["SAP_ADT_DESTINATION"])
    out = asyncio.run(release_transport(gw, sys.argv[1]))
    print("released:", out["released"], "| status:", out.get("status"))
    for m in out.get("messages", []):
        print("  ", m)
