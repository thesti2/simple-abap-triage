"""Fix sub-agent: `create_agent` with GUARDED ADT tools.

The LLM only decides *what* to change. Guardrails in code:
- it can touch only the objects in `allowed` (the alert's program / dump include, Z*/Y* only),
- every edit is recorded in the transport chosen by the worker (the agent never picks or releases TRs),
- at most `max_edits` edits, and activation only after a clean syntax check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool

from app.fix.adt import AdtError, AdtGateway

SYSTEM_PROMPT = """You are a senior ABAP developer fixing a bug in an SAP DEV system.
You get the alert (short dump), the triage root cause and the reproduce result. Make the SMALLEST correct code change
that removes the root cause without changing intended behaviour:
- read the source first; locate the failing line (the dump line number is given),
- prefer guarding / validating input and giving a clear message over silently changing results
  (e.g. check a divisor for 0 and raise a meaningful MESSAGE or skip the calculation),
- the program runs as a BACKGROUND JOB and the reproduce run must FINISH after your fix. A MESSAGE of TYPE 'E', 'A'
  or 'X' cancels a background job - do not use them for the handled case. Instead write a clear message that ends up
  in the job log (e.g. MESSAGE '...' TYPE 'S' DISPLAY LIKE 'W' or TYPE 'I'), skip the failing logic and continue/return,
- keep the style of the existing code, add a short comment mentioning the triage alert,
- edit with exact old/new strings (old string must be unique, include surrounding lines),
- run check_syntax; fix errors; then call activate once the check is clean,
- do not touch any other object, do not remove intentional demo modes other than the bug itself.
When done, reply with 2-4 sentences: what you changed and why."""


@dataclass
class FixSession:
    """Mutable record of what the agent did (for the report and guardrails)."""
    allowed: set[str]
    transport: str
    max_edits: int = 5
    edits: list[dict[str, str]] = field(default_factory=list)
    checked_clean: bool = False
    activated: bool = False
    log: list[str] = field(default_factory=list)


def build_fix_tools(adt: AdtGateway, s: FixSession) -> list[BaseTool]:
    def _guard(name: str) -> str | None:
        n = name.strip().upper()
        if n not in s.allowed:
            return f"REFUSED: {n} is not one of the objects you may change: {sorted(s.allowed)}"
        return None

    @tool
    async def read_source(object_name: str) -> str:
        """Read the current (saved) source of an ABAP program/include. Each line is prefixed with
        '<line number>| ' - that prefix is NOT part of the code; never put it into old_string."""
        if err := _guard(object_name):
            return err
        try:
            src = await adt.read_source(object_name.upper())
        except AdtError as e:
            return f"ERROR: {e}"
        s.log.append(f"read {object_name}")
        return "\n".join(f"{i:4d}| {line}" for i, line in enumerate(src.splitlines(), 1))

    @tool
    async def edit_source(object_name: str, old_string: str, new_string: str) -> str:
        """Replace an exact, unique piece of code by new code. old_string must match the source verbatim
        (same indentation, no line-number prefixes). Prefer 1-3 complete lines."""
        if err := _guard(object_name):
            return err
        if len(s.edits) >= s.max_edits:
            return f"REFUSED: edit limit ({s.max_edits}) reached - stop and summarise."
        try:
            res = await adt.edit_source(object_name.upper(), old_string, new_string, s.transport)
        except AdtError as e:
            s.log.append(f"edit {object_name} failed: {str(e)[:120]}")
            return f"ERROR: {e}\nRe-read the source with read_source and copy old_string exactly."
        ok = "successful" in res.lower()
        if ok:
            s.edits.append({"object": object_name.upper(), "old": old_string, "new": new_string})
            s.checked_clean = s.activated = False
        s.log.append(f"edit {object_name}: {res[:120]}")
        return res

    @tool
    async def check_syntax(object_name: str) -> str:
        """Syntax-check the (saved, inactive) source."""
        if err := _guard(object_name):
            return err
        try:
            res = await adt.check(object_name.upper())
        except AdtError as e:
            s.checked_clean = False
            return f"ERROR: {e}"
        s.checked_clean = "successful" in res.lower() and "error" not in res.lower()
        s.log.append(f"check {object_name}: {res[:120]}")
        return res

    @tool
    async def activate(object_name: str) -> str:
        """Activate the object. Only allowed after a clean check_syntax."""
        if err := _guard(object_name):
            return err
        if not s.checked_clean:
            return "REFUSED: run check_syntax first and fix all errors."
        try:
            res = await adt.activate(object_name.upper())
        except AdtError as e:
            return f"ERROR: {e}"
        s.activated = "successful" in res.lower()
        s.log.append(f"activate {object_name}: {res[:120]}")
        return res

    return [read_source, edit_source, check_syntax, activate]


async def run_fix_agent(llm: BaseChatModel, adt: AdtGateway, session: FixSession, task: str,
                        metadata: dict | None = None) -> str:
    agent = create_agent(llm, build_fix_tools(adt, session), system_prompt=SYSTEM_PROMPT, name="fix_agent")
    result = await agent.ainvoke({"messages": [HumanMessage(task)]},
                                 config={"recursion_limit": 30, "run_name": "fix_agent", "metadata": metadata or {}})
    return str(result["messages"][-1].content)
