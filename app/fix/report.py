"""Test document: HTML report + before/after PNGs (real SAP screens via WebGUI, else rendered from the job output)."""

from __future__ import annotations

import base64
import difflib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from app.fix.evidence import Evidence


class FixReport(BaseModel):
    """Structured RCA written by the LLM from the evidence (never invents: only from given data)."""
    root_cause: str = Field(description="Root cause analysis, 2-4 sentences, specific (program, line, input)")
    change_summary: str = Field(description="What was changed in the code and why, 1-3 sentences")
    test_summary: str = Field(description="Before vs after result, 1-2 sentences")
    risk_notes: str = Field(description="Side effects / what a reviewer should check, 1-2 sentences")


@dataclass
class ReportFiles:
    html: Path
    before_png: Path
    after_png: Path


def unified_diff(name: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile=f"{name} (before)", tofile=f"{name} (after)", n=3))


def _console_lines(ev: Evidence) -> list[str]:
    lines = [f"Program {ev.program}  Variant {ev.variant}  Job {ev.jobname}/{ev.jobcount}",
             f"Result: {ev.label}", "", "Job log:"]
    lines += [f"  {line}" for line in ev.joblog] or ["  -"]
    if ev.dump.get("runtime_error"):
        lines += ["", f"Short dump: {ev.dump.get('runtime_error')} in {ev.dump.get('program')} line {ev.dump.get('line')}"]
    if ev.spool:
        lines += ["", "List output (spool):"] + [f"  {line}" for line in ev.spool[:40]]
    return lines


def render_screenshot(ev: Evidence, title: str, path: Path) -> Path:
    """Draw the job result like a terminal window (SAP GUI screenshots are not possible from a worker)."""
    lines = [title, ""] + _console_lines(ev)
    try:
        font = ImageFont.truetype("consola.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    width = max(820, min(1600, 9 * max(len(line) for line in lines) + 40))
    height = 22 * len(lines) + 40
    img = Image.new("RGB", (width, height), "#1e1e1e")
    d = ImageDraw.Draw(img)
    color = "#4ec9b0" if ev.ok else "#f48771"
    d.rectangle([0, 0, width, 30], fill="#333333")
    d.text((12, 7), title, fill="#ffffff", font=font)
    for i, line in enumerate(lines[2:], start=0):
        fill = color if line.startswith("Result:") or line.startswith("Short dump") else "#d4d4d4"
        d.text((14, 40 + 22 * i), line, fill=fill, font=font)
    img.save(path)
    return path


def _img(path: Path, alt: str) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f'<p><img alt="{html.escape(alt)}" src="data:image/png;base64,{data}"></p>'


def build_report(out_dir: Path, alert: dict, before: Evidence, after: Evidence, diff: str, rca: FixReport,
                 transport: str, atc_summary: str, agent_notes: str,
                 before_screen: Path | None = None, after_screen: Path | None = None) -> ReportFiles:
    """before_screen/after_screen: real SAP screenshots (WebGUI); without them the job result is drawn as text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = alert["alert_id"].replace("/", "_")[-60:]
    before_png = before_screen or render_screenshot(
        before, f"BEFORE fix - {before.program} / {before.variant}", out_dir / f"{slug}_before.png")
    after_png = after_screen or render_screenshot(
        after, f"AFTER fix - {after.program} / {after.variant}", out_dir / f"{slug}_after.png")

    def pre(text: str) -> str:
        return f"<pre>{html.escape(text)}</pre>"

    def screen(path: Path | None, label: str) -> str:
        return _img(path, f"SAP screen {label} the fix") + "<p>Background job result:</p>" if path else ""

    def diff_html(text: str) -> str:
        rows = []
        for line in text.splitlines():
            cls = "add" if line.startswith("+") and not line.startswith("+++") else \
                  "del" if line.startswith("-") and not line.startswith("---") else ""
            rows.append(f'<span class="{cls}">{html.escape(line)}</span>')
        return "<pre>" + "\n".join(rows) + "</pre>"

    verdict = "PASSED" if (not before.ok and after.ok) else "FAILED"
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Fix test document - {html.escape(alert['alert_id'])}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1000px;margin:24px auto;color:#222}}
h1{{font-size:22px}} h2{{font-size:17px;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:28px}}
table{{border-collapse:collapse}} td{{padding:3px 12px 3px 0;vertical-align:top}}
img{{max-width:100%;border:1px solid #ccc}} pre{{background:#1e1e1e;color:#d4d4d4;padding:12px;overflow-x:auto;font-size:12.5px}}
.add{{color:#6a9955}} .del{{color:#f48771}} .ok{{color:#0a7a3e;font-weight:600}} .bad{{color:#b3261e;font-weight:600}}</style></head><body>
<h1>Fix test document</h1>
<table>
<tr><td>Alert</td><td><code>{html.escape(alert['alert_id'])}</code></td></tr>
<tr><td>Program / variant</td><td><code>{html.escape(before.program)}</code> / <code>{html.escape(before.variant)}</code></td></tr>
<tr><td>Error</td><td><code>{html.escape(alert.get('runtime_error') or alert.get('error_message') or '-')}</code></td></tr>
<tr><td>Transport request</td><td><code>{html.escape(transport)}</code> (not released - waiting for approval)</td></tr>
<tr><td>Test result</td><td class="{'ok' if verdict == 'PASSED' else 'bad'}">{verdict}: before = {before.label}, after = {after.label}</td></tr>
<tr><td>Generated</td><td>{datetime.now(UTC):%Y-%m-%d %H:%M} UTC by the ABAP Triage fix agent</td></tr>
</table>
<h2>Root cause analysis</h2><p>{html.escape(rca.root_cause)}</p>
<h2>What was changed</h2><p>{html.escape(rca.change_summary)}</p>{diff_html(diff or '(no change)')}
<h2>Test: before the fix</h2>{screen(before_screen, 'before')}{pre(chr(10).join(_console_lines(before)))}
<h2>Test: after the fix</h2>{screen(after_screen, 'after')}{pre(chr(10).join(_console_lines(after)))}
<p>{html.escape(rca.test_summary)}</p>
<h2>Code quality (ATC)</h2><p>{html.escape(atc_summary)}</p>
<h2>Reviewer notes</h2><p>{html.escape(rca.risk_notes)}</p>
<h2>Agent notes</h2><p>{html.escape(agent_notes)}</p>
</body></html>"""
    path = out_dir / f"{slug}_fix_report.html"
    path.write_text(doc, encoding="utf-8")
    return ReportFiles(path, before_png, after_png)
