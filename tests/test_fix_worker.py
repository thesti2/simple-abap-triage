import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.fix.evidence import TEST_INCLUDE, parse_unit_output, render_test_include
from app.fix.fix_agent import FixSession, build_fix_tools
from app.fix.report import FixReport
from app.fix.worker import FixWorker
from tests.conftest import triaged

BUGGY = "REPORT ztriage_demo_bug.\n  lv_unit_price = p_total / p_qty.\n"
FIXED = "REPORT ztriage_demo_bug.\n  IF p_qty = 0. MESSAGE 'Quantity 0' TYPE 'S'. RETURN. ENDIF.\n  lv_unit_price = p_total / p_qty.\n"


def _unit_output(status: str, runtime_error: str = "") -> str:
    data = {"jobname": "ZTRIAGE_RUN_ZTRIAGE_DEMO_BUG", "jobcount": "1", "status": status,
            "joblog": ["Job started", "Job canceled" if status == "A" else "Job finished"],
            "spool": [] if status == "A" else ["Unit price : 100"],
            "dump": {"runtime_error": runtime_error, "program": "ZTRIAGE_DEMO_BUG", "line": "55" if runtime_error else ""},
            "summary": f"status {status}"}
    return ("[FAILURE] zcl_triage_evidence\n  Critical Assertion Error: 'TRIAGE_EVIDENCE'\n  Details:\n    '@@EVIDENCE@@"
            + json.dumps(data) + "@@END@@'\n")


class FakeAdt:
    """Simulates the SAP side: a program whose job dumps while the bug is in the source."""

    def __init__(self, source: str = BUGGY, locked_tr: str | None = "XYZK900001", fix_works: bool = True):
        self.source, self.inactive = source, source
        self.locked_tr, self.fix_works = locked_tr, fix_works
        self.test_include = ""
        self.edits: list[tuple] = []
        self.created_tr = None

    async def read_file(self, aff):
        assert aff == TEST_INCLUDE
        return self.test_include

    async def write_file(self, aff, old, new, transport=""):
        assert old == self.test_include
        self.test_include = new
        return "Edit successful."

    async def activate(self, name, obj_type="PROG"):
        if obj_type == "PROG":
            self.source = self.inactive
        return "Activation successful"

    release_ok = True

    async def run_unit_tests(self, name, obj_type="CLAS"):
        if "Z_TRIAGE_TR_RELEASE" in self.test_include:
            data = ({"released": "X", "status": "R", "messages": ["S: XYZK900001 released"]} if self.release_ok
                    else {"released": "", "status": "D", "messages": ["E: Release of XYZK900001 failed (rc 8): object locked"]})
            return "Critical Assertion Error: 'TRIAGE_RELEASE' Details: '@@EVIDENCE@@" + json.dumps(data) + "@@END@@'"
        buggy = "p_total / p_qty" in self.source and ("IF p_qty = 0" not in self.source or not self.fix_works)
        return _unit_output("A", "COMPUTE_INT_ZERODIVIDE") if buggy else _unit_output("F")

    async def transports_for(self, name, obj_type="PROG"):
        if self.locked_tr:
            return True, [{"transportRequestNumber": self.locked_tr}]
        return True, [{"transportRequestNumber": "XYZK1"}, {"transportRequestNumber": "XYZK2"}]

    async def create_transport(self, name, description, obj_type="PROG"):
        self.created_tr = "XYZK900099"
        return self.created_tr

    async def read_source(self, name, obj_type="PROG"):
        return self.inactive

    async def edit_source(self, name, old, new, transport, obj_type="PROG"):
        if old not in self.inactive:
            return "Error: old string not found"
        self.inactive = self.inactive.replace(old, new, 1)
        self.edits.append((name, transport))
        return "Edit successful."

    async def check(self, name, obj_type="PROG"):
        return "Check successful"

    async def atc(self, name, obj_type="PROG"):
        return {"summaryInformation": "0 findings"}


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        class _S:
            def invoke(self, *a, **k):
                return FixReport(root_cause="P_QTY = 0 divides by zero at line 55.", change_summary="Added a zero check.",
                                 test_summary="Before: dump. After: finished.", risk_notes="Check message text.")
        return _S()


def _fix_script(edit_old="  lv_unit_price = p_total / p_qty.\n", activate=True):
    msgs = [
        AIMessage(content="", tool_calls=[{"name": "read_source", "args": {"object_name": "ZTRIAGE_DEMO_BUG"}, "id": "1"}]),
        AIMessage(content="", tool_calls=[{"name": "edit_source", "id": "2", "args": {
            "object_name": "ZTRIAGE_DEMO_BUG", "old_string": edit_old,
            "new_string": "  IF p_qty = 0. MESSAGE 'Quantity 0' TYPE 'S'. RETURN. ENDIF.\n" + edit_old}}]),
        AIMessage(content="", tool_calls=[{"name": "check_syntax", "args": {"object_name": "ZTRIAGE_DEMO_BUG"}, "id": "3"}]),
    ]
    if activate:
        msgs.append(AIMessage(content="", tool_calls=[{"name": "activate", "args": {"object_name": "ZTRIAGE_DEMO_BUG"}, "id": "4"}]))
    msgs.append(AIMessage(content="Added a zero check for P_QTY before the division."))
    return ScriptedModel(messages=iter(msgs))


@pytest.fixture
def fix_alert(repo):
    aid = triaged(repo, "div0")
    repo.update_fields(aid, chat_channel="C1", chat_thread="1.1")
    repo.transition(aid, "FIX_REQUESTED", actor="slack:U1")
    return aid


def _worker(repo, chat, adt, model, tmp_path, max_attempts=1):
    return FixWorker(repo, adt, model, chat, report_dir=tmp_path / "reports", abap_copy_dir=None,
                     activation_wait_s=0, stale_retries=1, max_attempts=max_attempts)


# ---------------------------------------------------------------- evidence
def test_test_include_template_and_injection_guard():
    src = render_test_include("ztriage_demo_bug", "zbug_div0")
    assert "iv_program = 'ZTRIAGE_DEMO_BUG' iv_variant = 'ZBUG_DIV0'" in src and "@@EVIDENCE@@" in src
    with pytest.raises(ValueError):
        render_test_include("ZX' ). DELETE FROM mara. ( '", "V")


def test_parse_unit_output():
    ev = parse_unit_output("P", "V", _unit_output("A", "COMPUTE_INT_ZERODIVIDE"))
    assert ev.status == "A" and not ev.ok and ev.dump["runtime_error"] == "COMPUTE_INT_ZERODIVIDE"
    with pytest.raises(ValueError):
        parse_unit_output("P", "V", "Overall Test Run Status: [PASSED]")


# ---------------------------------------------------------------- guardrails
def _tools(adt, **kw):
    s = FixSession(allowed={"ZTRIAGE_DEMO_BUG"}, transport="XYZK900001", **kw)
    return s, {t.name: t for t in build_fix_tools(adt, s)}


def test_guard_refuses_other_objects():
    adt = FakeAdt()
    s, t = _tools(adt)
    out = asyncio.run(t["edit_source"].ainvoke({"object_name": "SAPMV45A", "old_string": "a", "new_string": "b"}))
    assert out.startswith("REFUSED") and adt.edits == []


def test_activate_requires_clean_check_and_edits_use_worker_transport():
    adt = FakeAdt()
    s, t = _tools(adt)
    asyncio.run(t["edit_source"].ainvoke({"object_name": "ztriage_demo_bug", "old_string": "p_total / p_qty",
                                          "new_string": "p_total / 1"}))
    assert adt.edits == [("ZTRIAGE_DEMO_BUG", "XYZK900001")]
    assert asyncio.run(t["activate"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG"})).startswith("REFUSED")
    asyncio.run(t["check_syntax"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG"}))
    assert "successful" in asyncio.run(t["activate"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG"}))


def test_tool_errors_go_back_to_the_agent():
    from app.fix.adt import AdtError

    class _Failing(FakeAdt):
        async def edit_source(self, *a, **k):
            raise AdtError("[OLD_STRING_NOT_FOUND] 'oldString' was not found")

    s, t = _tools(_Failing())
    out = asyncio.run(t["edit_source"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG", "old_string": "x", "new_string": "y"}))
    assert out.startswith("ERROR") and "Re-read" in out and s.edits == []


def test_read_source_marks_line_numbers():
    s, t = _tools(FakeAdt())
    assert "   2| " in asyncio.run(t["read_source"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG"}))


def test_retry_reuses_previous_fix_transport(repo, chat, tmp_path):
    adt = FakeAdt(locked_tr=None)
    w = _worker(repo, chat, adt, _fix_script(), tmp_path)
    assert asyncio.run(w.choose_transport("ZTRIAGE_DEMO_BUG", "A1", previous="XYZK2")) == "XYZK2"
    assert adt.created_tr is None


def test_edit_limit():
    adt = FakeAdt()
    s, t = _tools(adt, max_edits=1)
    asyncio.run(t["edit_source"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG", "old_string": "REPORT", "new_string": "REPORT"}))
    out = asyncio.run(t["edit_source"].ainvoke({"object_name": "ZTRIAGE_DEMO_BUG", "old_string": "x", "new_string": "y"}))
    assert "limit" in out


# ---------------------------------------------------------------- worker flow
def test_worker_happy_path(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt()
    status = asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).process(fix_alert))
    assert status == "FIX_READY"
    a = repo.get(fix_alert)
    assert a.status == "FIX_READY" and a.fix_tr == "XYZK900001"          # object was locked -> that TR
    assert [t for _, t in chat.files] == ["Before fix", "After fix", "Fix test document"]
    assert "Fix ready for review" in chat.replies[-1][1]
    html = next((tmp_path / "reports").glob("*_fix_report.html")).read_text(encoding="utf-8")
    assert "PASSED: before = CANCELLED, after = FINISHED" in html and "+  IF p_qty = 0" in html
    assert [e.to_status for e in repo.history(fix_alert)][-2:] == ["FIX_IN_PROGRESS", "FIX_READY"]


def test_worker_creates_transport_when_not_locked(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt(locked_tr=None)
    asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).process(fix_alert))
    assert repo.get(fix_alert).fix_tr == "XYZK900099" and adt.edits[0][1] == "XYZK900099"


def test_worker_cannot_reproduce(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt(source=FIXED)
    status = asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).process(fix_alert))
    assert status == "FIX_FAILED" and "could not reproduce" in chat.replies[-1][1] and adt.edits == []


def test_worker_agent_did_not_activate(repo, chat, fix_alert, tmp_path):
    status = asyncio.run(_worker(repo, chat, FakeAdt(), _fix_script(activate=False), tmp_path).process(fix_alert))
    assert status == "FIX_FAILED" and "did not produce an activated change" in chat.replies[-1][1]


def test_worker_fix_does_not_help(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt(fix_works=False)
    status = asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).process(fix_alert))
    assert status == "FIX_FAILED" and "still ends CANCELLED" in chat.replies[-1][1]
    assert len(chat.files) == 3  # report is still uploaded for the developer


def test_stale_program_load_is_retried(repo, chat, fix_alert, tmp_path):
    class _Stale(FakeAdt):
        """First after-run still uses the old program load (same dump), second one the new code."""
        def __init__(self):
            super().__init__()
            self.after_runs = 0

        async def run_unit_tests(self, name, obj_type="CLAS"):
            if "IF p_qty = 0" in self.source:
                self.after_runs += 1
                if self.after_runs == 1:
                    return _unit_output("A", "COMPUTE_INT_ZERODIVIDE")
            return await super().run_unit_tests(name, obj_type)

    adt = _Stale()
    assert asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).process(fix_alert)) == "FIX_READY"
    assert adt.after_runs == 2


def test_second_attempt_gets_after_result(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt(fix_works=False)
    first = _fix_script().messages
    second = _fix_script().messages
    model = ScriptedModel(messages=iter(list(first) + list(second)))
    status = asyncio.run(_worker(repo, chat, adt, model, tmp_path, max_attempts=2).process(fix_alert))
    assert status == "FIX_FAILED"
    assert any("Attempt 1" in text for _, text in chat.replies)


# ---------------------------------------------------------------- 3.5 release
def _approved(repo, aid, tr="XYZK900001"):
    for s in ("FIX_IN_PROGRESS", "FIX_READY"):
        repo.transition(aid, s)
    repo.transition(aid, "APPROVED", actor="slack:U9", approved_by="U9", fix_tr=tr)


def test_release_after_approval(repo, chat, fix_alert, tmp_path):
    _approved(repo, fix_alert)
    adt = FakeAdt()
    w = _worker(repo, chat, adt, _fix_script(), tmp_path)
    w.uat_sid = "UAT"
    assert asyncio.run(w.release(fix_alert)) == "RELEASED"
    assert "iv_trkorr   = 'XYZK900001'" in adt.test_include
    assert repo.get(fix_alert).status == "RELEASED"
    assert "released (approved by <@U9>) - ready for import to UAT" in chat.replies[-1][1]


def test_release_failure_keeps_approved(repo, chat, fix_alert, tmp_path):
    _approved(repo, fix_alert)
    adt = FakeAdt()
    adt.release_ok = False
    assert asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).release(fix_alert)) == "APPROVED"
    assert "was not released" in chat.replies[-1][1] and "object locked" in chat.replies[-1][1]


def test_release_only_when_approved(repo, chat, fix_alert, tmp_path):
    adt = FakeAdt()
    assert asyncio.run(_worker(repo, chat, adt, _fix_script(), tmp_path).release(fix_alert)) == "FIX_REQUESTED"
    assert adt.test_include == ""  # nothing was prepared / released


def test_release_template_rejects_bad_tr():
    from app.fix.evidence import render_release_include

    with pytest.raises(ValueError):
        render_release_include("XYZK90' ). DELETE")


def test_worker_refuses_standard_program(repo, chat, fix_alert, tmp_path):
    repo.update_fields(fix_alert, program="SAPMV45A")
    status = asyncio.run(_worker(repo, chat, FakeAdt(), _fix_script(), tmp_path).process(fix_alert))
    assert status == "FIX_FAILED" and "not a custom" in chat.replies[-1][1]
