"""Local fix worker (runs on the workstation with Eclipse + ADT MCP).

  uv run python -m app.fix.worker                # poll for FIX_REQUESTED alerts
  uv run python -m app.fix.worker --once <id>    # process one alert and exit

Deterministic flow; the LLM is used only for the code change (fix agent) and the RCA text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.chat.base import ChatClient, ThreadRef
from app.chat.slack_blocks import approval_blocks
from app.db import Alert, Repository
from app.fix.adt import AdtGateway
from app.fix.evidence import EVIDENCE_CLASS, Evidence, parse_unit_output, render_test_include
from app.fix.fix_agent import FixSession, run_fix_agent
from app.fix.release import prepare_include, release_transport
from app.fix.report import FixReport, build_report, unified_diff
from app.status import Status

log = logging.getLogger("fix_worker")
ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports"
_TR_RE = re.compile(r"\b[A-Z0-9]{3}K\d{6}\b")


def root_tr_messages(messages: list[str], root_tr: str) -> list[str]:
    """Release messages for the chat: drop those naming another TR (the request's tasks), keep the rest."""
    return [m for m in messages if all(tr == root_tr for tr in _TR_RE.findall(m))]


class FixWorker:
    def __init__(self, repo: Repository, adt: AdtGateway, llm: BaseChatModel, chat: ChatClient,
                 report_dir: Path = REPORT_DIR, abap_copy_dir: Path | None = ROOT / "ABAP",
                 activation_wait_s: float = 45, stale_retries: int = 3, max_attempts: int = 2,
                 uat_sid: str = "", screens: Any = None) -> None:
        self.repo, self.adt, self.llm, self.chat = repo, adt, llm, chat
        self.screens = screens  # WebGuiCapture (or any object with async capture_run); None -> text images
        self.uat_sid = uat_sid
        self.report_dir, self.abap_copy_dir = report_dir, abap_copy_dir
        self.activation_wait_s, self.stale_retries, self.max_attempts = activation_wait_s, stale_retries, max_attempts

    # ---------------------------------------------------------------- helpers
    def _ref(self, a: Alert) -> ThreadRef | None:
        return ThreadRef(a.chat_channel, a.chat_thread) if a.chat_channel and a.chat_thread else None

    def _say(self, a: Alert, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        if ref := self._ref(a):
            self.chat.reply(ref, text, blocks)

    def _refresh_card(self, alert_id: str) -> None:
        a = self.repo.get(alert_id)
        if (ref := self._ref(a)) and hasattr(self.chat, "update_alert"):
            self.chat.update_alert(ref, a.to_dict())

    async def reproduce(self, program: str, variant: str) -> Evidence:
        """Run program+variant as a background job via the ABAP Unit evidence class."""
        await prepare_include(self.adt, render_test_include(program, variant))
        return parse_unit_output(program, variant, await self.adt.run_unit_tests(EVIDENCE_CLASS))

    async def reproduce_after_activation(self, program: str, variant: str, before: Evidence) -> Evidence:
        """Run the 'after' reproduce. With several application servers a background job can still pick up the old
        program load for a while after activation, so wait first and retry while the dump points at the old code."""
        await asyncio.sleep(self.activation_wait_s)
        after = await self.reproduce(program, variant)
        for _ in range(self.stale_retries):
            stale = (not after.ok and after.dump.get("runtime_error")
                     and after.dump.get("runtime_error") == before.dump.get("runtime_error")
                     and after.dump.get("line") == before.dump.get("line"))
            if not stale:
                break
            log.info("after-run looks like the old program load (same dump + line) - waiting and retrying")
            await asyncio.sleep(self.activation_wait_s)
            after = await self.reproduce(program, variant)
        return after

    async def capture_screen(self, alert_id: str, program: str, variant: str, label: str) -> Path | None:
        """Real SAP screenshot of a foreground run (optional evidence: a failure never fails the fix)."""
        if self.screens is None:
            return None
        slug = alert_id.replace("/", "_")[-60:]
        try:
            return await self.screens.capture_run(program, variant, self.report_dir / f"{slug}_{label}_sap.png")
        except Exception as e:  # noqa: BLE001
            log.warning("SAP screenshot (%s) failed, using the text image: %s", label, e)
            return None

    async def choose_transport(self, program: str, alert_id: str, previous: str | None = None) -> str:
        """Reuse the alert's fix TR on a retry; if the object is locked in a TR SAP forces that one;
        otherwise create a new TR."""
        _, trs = await self.adt.transports_for(program)
        numbers = [t["transportRequestNumber"] for t in trs]
        if len(numbers) == 1:
            return numbers[0]
        if previous and previous in numbers:
            return previous
        return await self.adt.create_transport(program, f"Triage fix {program} ({alert_id[-20:]})")

    def write_rca(self, alert: dict[str, Any], before: Evidence, after: Evidence, diff: str, notes: str) -> FixReport:
        prompt = (f"Alert: {json.dumps({k: alert.get(k) for k in ('alert_id', 'program', 'variant', 'runtime_error', 'error_message', 'root_cause')})}\n"
                  f"Before fix: status {before.label}, dump {before.dump}, job log {before.joblog}\n"
                  f"After fix: status {after.label}, job log {after.joblog}, list output {after.spool[:20]}\n"
                  f"Code diff:\n{diff}\nFix agent notes: {notes}")
        structured = self.llm.with_structured_output(FixReport, method="json_schema")
        return structured.invoke([SystemMessage("Write the RCA section of a fix test document for an SAP ABAP bug "
                                                "fix. Use only the facts given. Be specific and short."),
                                  HumanMessage(prompt)], config={"run_name": "fix_rca"})

    # ---------------------------------------------------------------- main flow
    async def process(self, alert_id: str) -> str:
        a = self.repo.transition(alert_id, Status.FIX_IN_PROGRESS, actor="fix-worker", note="fix agent started")
        self._refresh_card(alert_id)
        program, variant = (a.program or "").upper(), (a.variant or "").upper()
        try:
            if not program.startswith(("Z", "Y")):
                raise RuntimeError(f"{program} is not a custom (Z/Y) program")
            self._say(a, f":robot_face: Fix agent started: reproducing `{program}` / `{variant}` in DEV…")

            before = await self.reproduce(program, variant)
            if before.ok:
                raise RuntimeError(f"could not reproduce the error - `{program}` / `{variant}` finished normally")
            before_screen = await self.capture_screen(alert_id, program, variant, "before")

            transport = await self.choose_transport(program, alert_id, previous=a.fix_tr)
            self.repo.update_fields(alert_id, fix_tr=transport)
            source_before = await self.adt.read_source(program)

            p = a.payload or {}
            allowed = {program} | {x.upper() for x in (p.get("error_program"), p.get("error_include"))
                                   if x and x.upper().startswith(("Z", "Y"))}
            session = FixSession(allowed=allowed, transport=transport)
            task = (f"Alert {alert_id}: job {a.job_name} cancelled.\n"
                    f"Program {program}, variant {variant}. Runtime error {a.runtime_error or '-'} "
                    f"in {p.get('error_program') or program} include {p.get('error_include') or '-'} "
                    f"line {p.get('error_line') or '?'}. Error message: {a.error_message or '-'}\n"
                    f"Triage root cause: {a.root_cause}\nTriage recommendation: {a.recommendation}\n"
                    f"Reproduce run (before): {before.label}; dump {before.dump}; job log {before.joblog}\n"
                    f"Objects you may change: {sorted(allowed)}.")
            notes, after = "", before
            for attempt in range(1, self.max_attempts + 1):
                notes = await run_fix_agent(self.llm, self.adt, session, task, metadata={"alert_id": alert_id,
                                                                                          "attempt": attempt})
                if not session.edits or not session.activated:
                    raise RuntimeError(f"fix agent did not produce an activated change. Notes: {notes[:300]}")
                after = await self.reproduce_after_activation(program, variant, before)
                if after.ok:
                    break
                self._say(a, f":repeat: Attempt {attempt}: the job still ends *{after.label}* - "
                             f"{'giving the agent the result for another try' if attempt < self.max_attempts else 'stopping'}.")
                task = (f"Your change was activated but the reproduce run still ends {after.label}.\n"
                        f"Job log after your change: {after.joblog}\nDump: {after.dump}\n"
                        f"Re-read the source and correct your change (same rules). Original task:\n{task}")
                session.activated = session.checked_clean = False
            after_screen = await self.capture_screen(alert_id, program, variant, "after") if after.ok else None
            source_after = await self.adt.read_source(program)
            diff = unified_diff(program, source_before, source_after)
            atc = await self.adt.atc(program)
            atc_summary = str(atc.get("summaryInformation") or "no findings").replace("\n", "; ")
            rca = self.write_rca(a.to_dict(), before, after, diff, notes)
            files = build_report(self.report_dir, a.to_dict(), before, after, diff, rca, transport, atc_summary, notes,
                                 before_screen=before_screen, after_screen=after_screen)
            self._copy_source(program, source_after)

            if (ref := self._ref(a)) is not None:
                for f, title in ((files.before_png, "Before fix"), (files.after_png, "After fix"),
                                 (files.html, "Fix test document")):
                    self.chat.upload_file(ref, f, title)

            if not after.ok:
                raise RuntimeError(f"after the change the job still ends {after.label} "
                                   f"({after.dump.get('runtime_error') or 'see job log'}) - change left in {transport}")

            self.repo.transition(alert_id, Status.FIX_READY, actor="fix-worker", note=f"fix ready in {transport}")
            self._refresh_card(alert_id)
            text = (f":white_check_mark: *Fix ready for review* - TR `{transport}` (not released)\n"
                    f"*Root cause:* {rca.root_cause}\n*Change:* {rca.change_summary}\n*Test:* {rca.test_summary}\n"
                    f"*Please check:* {rca.risk_notes}\nATC: {atc_summary}")
            self._say(a, text, approval_blocks(alert_id, text))
            return Status.FIX_READY
        except Exception as e:  # noqa: BLE001 - report every failure in the thread
            log.exception("fix failed for %s", alert_id)
            self.repo.transition(alert_id, Status.FIX_FAILED, actor="fix-worker", note=str(e)[:500])
            self._refresh_card(alert_id)
            self._say(self.repo.get(alert_id), f":x: *Fix failed:* {e}\nYou can ask me to try again with "
                                                f"`@abap-triage please fix it`.")
            return Status.FIX_FAILED

    async def release(self, alert_id: str) -> str:
        """Phase 3.5: an APPROVED alert -> release its fix TR (tasks + request). Import to UAT stays with TMS/Basis."""
        a = self.repo.get(alert_id)
        if a.status != Status.APPROVED:
            return a.status
        if not a.fix_tr:
            self._say(a, ":warning: Approved, but no fix transport is recorded - nothing to release.")
            return a.status
        try:
            result = await release_transport(self.adt, a.fix_tr)
        except Exception as e:  # noqa: BLE001 - keep APPROVED, report, retry possible
            log.exception("release failed for %s", alert_id)
            self._say(a, f":x: Releasing TR `{a.fix_tr}` failed: {e}\nStatus stays *APPROVED*; it will be retried.")
            return a.status
        if not result["released"]:
            msgs = "; ".join(root_tr_messages(result.get("messages", []), a.fix_tr)) or "no details"
            self._say(a, f":x: TR `{a.fix_tr}` was not released: {msgs}\nStatus stays *APPROVED* - "
                         f"please check the TR in SE09/SE10.")
            return a.status
        approver = f"<@{a.approved_by}>" if a.approved_by else "the approver"
        self.repo.transition(alert_id, Status.RELEASED, actor="fix-worker",
                             note=f"TR {a.fix_tr} released (status {result.get('status')}); approved by {a.approved_by}")
        self._refresh_card(alert_id)
        uat = f" to {self.uat_sid}" if self.uat_sid else ""
        # only the root TR is named in the chat - its tasks are an SAP detail (AGENTS.md "Improvements")
        self._say(a, f":rocket: TR `{a.fix_tr}` released (approved by {approver}) - ready for import{uat}.")
        return Status.RELEASED

    def _copy_source(self, program: str, source: str) -> None:
        if self.abap_copy_dir is None:
            return
        target = self.abap_copy_dir / f"{program.lower()}.prog.abap"
        if target.exists():  # only keep copies of objects we track in the repo
            target.write_text(source if source.endswith("\n") else source + "\n", encoding="utf-8")

    async def run_forever(self, interval_s: int = 15) -> None:
        log.info("fix worker polling every %ss", interval_s)
        failed_releases: dict[str, int] = {}
        while True:
            for a in self.repo.list_alerts(statuses=[Status.FIX_REQUESTED], limit=5):
                log.info("fixing %s", a.alert_id)
                await self.process(a.alert_id)
            for a in self.repo.list_alerts(statuses=[Status.APPROVED], limit=5):
                if failed_releases.get(a.alert_id, 0) >= 3:  # don't spam the thread; needs a human
                    continue
                log.info("releasing TR of %s", a.alert_id)
                if await self.release(a.alert_id) != Status.RELEASED:
                    failed_releases[a.alert_id] = failed_releases.get(a.alert_id, 0) + 1
            await asyncio.sleep(interval_s)


def _build() -> FixWorker:
    import os

    from app.chat.slack import SlackChatClient
    from app.config import get_settings
    from app.fix.webgui import capture_from_env
    from app.llm import get_llm

    s = get_settings()
    adt = AdtGateway(os.environ["SAP_ADT_MCP_URL"], os.environ["SAP_ADT_MCP_AUTH"], os.environ["SAP_ADT_DESTINATION"])
    screens = capture_from_env()
    log.info("SAP screenshots: %s", "WebGUI" if screens else "off (text images) - SAP_WEBGUI_* not set")
    if api_url := os.environ.get("TRIAGE_API_URL", "").strip():
        from app.worker_api import RemoteRepository

        repo: Any = RemoteRepository(api_url, os.environ["WORKER_API_TOKEN"])
        log.info("alert store: worker API %s", api_url)
    else:
        repo = Repository(s.db_url)
        log.info("alert store: database (%s)", s.db_url.split(":", 1)[0])
    return FixWorker(repo, adt, get_llm(s), SlackChatClient(s.slack_bot_token, s.slack_channel_id),
                     uat_sid=s.sap_uat_sid, screens=screens)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", metavar="ALERT_ID")
    args = ap.parse_args()
    w = _build()
    asyncio.run(w.process(args.once) if args.once else w.run_forever())
