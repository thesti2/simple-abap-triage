"""Phase 0 smoke test: does MODEL (via OpenRouter) support tool calling and structured output?

Run: uv run python scripts/smoke_openrouter.py
Prints only model capabilities and short answers - never keys.
"""

from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import get_settings
from app.llm import get_llm


class Verdict(BaseModel):
    category: Literal["DEVELOPER", "FUNCTIONAL", "BASIS"] = Field(description="Who should handle it")
    reason: str = Field(description="One sentence")


@tool
def get_alert(alert_id: str) -> str:
    """Return the stored alert for an alert id."""
    return f"alert {alert_id}: COMPUTE_INT_ZERODIVIDE in ZTRIAGE_DEMO_BUG line 55"


def main() -> None:
    s = get_settings()
    llm = get_llm(s)
    print(f"model: {s.model}")

    r = llm.invoke("Reply with the single word: pong")
    print(f"[plain]   ok -> {r.content.strip()[:40]!r}")

    r = llm.bind_tools([get_alert]).invoke("Look up alert A-1 using the tool.")
    calls = getattr(r, "tool_calls", []) or []
    print(f"[tools]   {'ok' if calls else 'NO TOOL CALL'} -> {[c['name'] for c in calls]}")

    q = "SAP background job dumped with COMPUTE_INT_ZERODIVIDE in a custom Z program. Who handles it?"
    for method in ("function_calling", "json_schema"):
        try:
            v = llm.with_structured_output(Verdict, method=method).invoke(q)
            print(f"[struct:{method}] ok -> {v.category}: {v.reason[:70]}")
        except Exception as e:  # noqa: BLE001 - smoke test reports any failure
            print(f"[struct:{method}] FAILED -> {type(e).__name__}: {str(e)[:120]}")


if __name__ == "__main__":
    main()
