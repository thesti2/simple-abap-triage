"""Thin async gateway to the local Eclipse ADT MCP server (the only way the worker touches SAP)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient


def _text(result: Any) -> str:
    """MCP tool results come back as str or a list of content blocks - normalise to text."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in result]
        return "\n".join(parts)
    return str(result)


class AdtError(RuntimeError):
    pass


class AdtGateway:
    def __init__(self, url: str, auth: str, destination: str) -> None:
        self._client = MultiServerMCPClient(
            {"sap-adt": {"transport": "streamable_http", "url": url, "headers": {"Authorization": auth}}}
        )
        self.destination = destination

    async def _call(self, name: str, **args: Any) -> str:
        """Call an MCP tool directly on a session. Some ADT tools return their data only as structured content
        (the text part is just a hint), so structured content wins over text."""
        async with self._client.session("sap-adt") as session:
            res = await session.call_tool(name, {"destination": self.destination, **args})
        structured = getattr(res, "structuredContent", None)
        if structured:
            # some servers wrap the payload as {"result": ...}
            if set(structured) == {"result"}:
                structured = structured["result"]
            return structured if isinstance(structured, str) else json.dumps(structured)
        text = _text([c.model_dump() if hasattr(c, "model_dump") else c for c in (res.content or [])])
        if getattr(res, "isError", False):
            raise AdtError(f"{name}: {text[:500]}")
        return text

    @staticmethod
    def _obj(name: str, obj_type: str) -> dict[str, str]:
        return {"objectName": name, "objectType": obj_type}

    # ---------------------------------------------------------------- source
    async def read_source(self, name: str, obj_type: str = "PROG") -> str:
        raw = await self._call("abap_object_read_file", objectIdentifier=self._obj(name, obj_type), limit=5000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AdtError(f"read {name}: {raw[:300]}") from e
        if data.get("truncated"):
            raise AdtError(f"{name} has more than 5000 lines - not supported by the MVP")
        return data["content"]

    async def edit_source(self, name: str, old: str, new: str, transport: str, obj_type: str = "PROG") -> str:
        return await self._call("abap_object_edit_file", objectIdentifier=self._obj(name, obj_type),
                                oldString=old, newString=new, transportNumber=transport)

    async def write_file(self, aff_file: str, old: str, new: str, transport: str = "") -> str:
        args: dict[str, Any] = {"objectIdentifier": {"affFileName": aff_file}, "oldString": old, "newString": new}
        if transport:
            args["transportNumber"] = transport
        return await self._call("abap_object_edit_file", **args)

    async def read_file(self, aff_file: str) -> str:
        raw = await self._call("abap_object_read_file", objectIdentifier={"affFileName": aff_file}, limit=5000)
        return json.loads(raw)["content"]

    async def check(self, name: str, obj_type: str = "PROG") -> str:
        return await self._call("abap_object_check", objectIdentifiers=[self._obj(name, obj_type)])

    async def activate(self, name: str, obj_type: str = "PROG") -> str:
        return await self._call("abap_object_activate", objectIdentifiers=[self._obj(name, obj_type)])

    # ---------------------------------------------------------------- transports
    async def transports_for(self, name: str, obj_type: str = "PROG") -> tuple[bool, list[dict[str, str]]]:
        """(recording_required, candidate TRs). If the object is locked, the list holds only its TR."""
        raw = await self._call("abap_transport_get_request", isCreation=False,
                               objectIdentifier={"objectIdentifier": self._obj(name, obj_type)})
        data = json.loads(raw)
        return bool(data.get("isRecordingRequired")), data.get("transportRequests", [])

    async def create_transport(self, name: str, description: str, obj_type: str = "PROG") -> str:
        raw = await self._call("abap_transport_create_request", isCreation=False, shortDescription=description[:60],
                               objectIdentifier={"objectIdentifier": self._obj(name, obj_type)})
        tr = raw.strip().strip('"')
        if not re.fullmatch(r"[A-Z0-9]{3}K\d{6}", tr):
            raise AdtError(f"create transport: {raw[:200]}")
        return tr

    # ---------------------------------------------------------------- execution / quality
    async def run_unit_tests(self, name: str, obj_type: str = "CLAS") -> str:
        return await self._call("abap_unit_run_tests", objectIdentifiers=[self._obj(name, obj_type)])

    async def atc(self, name: str, obj_type: str = "PROG", timeout_s: int = 120) -> dict[str, Any]:
        start = json.loads(await self._call("abap_atc_run_start", objects=[self._obj(name, obj_type)]))
        wid = start["worklistId"]
        for _ in range(timeout_s // 3):
            res = await self._call("abap_atc_run_get_result", worklistId=wid)
            try:
                data = json.loads(res)
            except json.JSONDecodeError:
                data = {"summaryInformation": res}
            if data.get("objects") is not None or "findings were found" in str(data.get("summaryInformation", "")) \
                    or "No findings" in res:
                return data
            await asyncio.sleep(3)
        return {"summaryInformation": "ATC did not finish in time"}
