"""Real SAP screenshots via SAP GUI for HTML (WebGUI) + headless Chromium (Playwright).

Runs the program with its variant in the foreground (SA38 -> Execute with variant) and saves the result
screen: the short dump before the fix, the list output after it. Works wherever the worker can reach the
WebGUI URL (PC on VPN today, an internal VM or GCP with a network path later).

Optional: without SAP_WEBGUI_URL / _USER / _PASSWORD the worker keeps the text-rendered images.
The password is only typed into the logon form; it is never logged or written to a file.

  uv run python -m app.fix.webgui --probe                      # log on, capture the start screen, log off
  uv run python -m app.fix.webgui --run ZPROG ZVARIANT [--debug]   # capture one run (debug: every step)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

log = logging.getLogger("webgui")
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class WebGuiConfig:
    url: str
    user: str
    password: str
    language: str = "EN"

    @classmethod
    def from_env(cls) -> WebGuiConfig | None:
        url, user, pw = (os.environ.get(k, "").strip() for k in
                         ("SAP_WEBGUI_URL", "SAP_WEBGUI_USER", "SAP_WEBGUI_PASSWORD"))
        if not (url and user and pw):
            return None
        return cls(url, user, pw, os.environ.get("SAP_WEBGUI_LANGUAGE", "EN").strip() or "EN")

    def start_url(self, transaction: str) -> str:
        """WebGUI URL with logon language and start transaction (keeps sap-client etc. from the configured URL).
        saml2=disabled: skip the SAML redirect (IAS/Okta SSO, not automatable) and use the SAP password logon."""
        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query))
        query.setdefault("saml2", "disabled")
        query.update({"sap-language": self.language, "~transaction": transaction})
        return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))

    def logoff_url(self) -> str:
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, "/sap/public/bc/icf/logoff", "", ""))


class WebGuiError(RuntimeError):
    pass


class WebGuiCapture:
    """One browser + SAP logon per capture: simple, no session state left behind between fix runs."""

    def __init__(self, cfg: WebGuiConfig, headless: bool = True, timeout_ms: int = 60_000,
                 debug_dir: Path | None = None) -> None:
        self.cfg, self.headless, self.timeout_ms, self.debug_dir = cfg, headless, timeout_ms, debug_dir

    async def _settle(self, page) -> None:
        """Wait until WebGUI finished the server round trip (no portable 'ready' flag across releases)."""
        try:
            await page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except Exception:  # noqa: BLE001 - long polling on some releases; fall through to the fixed pause
            pass
        await page.wait_for_timeout(1500)

    async def _step(self, page, name: str) -> None:
        await self._settle(page)
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(self.debug_dir / f"step_{name}.png"))

    async def _logon(self, page, transaction: str) -> None:
        await page.goto(self.cfg.start_url(transaction), timeout=self.timeout_ms)
        await self._step(page, "01_logon_page")
        user = page.locator('input[name="sap-user"]')
        if await user.count():
            await user.fill(self.cfg.user)
            await page.locator('input[name="sap-password"]').fill(self.cfg.password)
            await page.keyboard.press("Enter")
            await self._step(page, "02_after_logon")
            if await page.locator('input[name="sap-password"]').count():
                raise WebGuiError("WebGUI logon failed (still on the logon page) - check user/password/client")
        # "user already logged on" / license popup: continue with this logon
        cont = page.get_by_role("button", name="Continue")
        if await cont.count():
            await cont.first.click()
            await self._step(page, "03_after_popup")

    async def _logoff(self, page) -> None:
        try:
            await page.goto(self.cfg.logoff_url(), timeout=15_000)
        except Exception:  # noqa: BLE001 - session times out on the server anyway
            log.info("WebGUI logoff did not complete")

    async def _with_page(self, transaction: str, action):
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            ctx = await browser.new_context(viewport={"width": 1400, "height": 900}, ignore_https_errors=True,
                                            locale=self.cfg.language.lower())
            page = await ctx.new_page()
            try:
                await self._logon(page, transaction)
                return await action(page)
            finally:
                await self._logoff(page)
                await browser.close()

    async def probe(self, path: Path) -> Path:
        """Log on and capture the start screen (read-only connectivity / credential check)."""
        async def shot(page):
            await self._settle(page)
            await page.screenshot(path=str(path))
            return path
        return await self._with_page("SESSION_MANAGER", shot)

    async def capture_run(self, program: str, variant: str, path: Path) -> Path:
        """SA38 with the program prefilled -> Shift+F6 (with variant) -> variant -> Enter -> F8 -> screenshot.
        The variant popup only loads the variant into the selection screen; F8 (button btn[8], language
        independent) then runs it. The SAP screen lives in the iframe #ITSFRAME1."""
        async def run(page):
            await self._step(page, "10_sa38")
            await page.keyboard.press("Shift+F6")
            await self._step(page, "11_variant_popup")
            await page.keyboard.type(variant)  # cursor is in the variant field of the popup
            await page.keyboard.press("Enter")
            await self._step(page, "12_selection_screen")
            frame = page.frame_locator("#ITSFRAME1")
            execute = frame.locator('[id$="::btn[8]"]')
            if not await execute.count():
                raise WebGuiError(f"no selection screen after loading variant {variant} - see the debug screenshots")
            await execute.first.click()
            await self._step(page, "13_result")
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
            return path
        return await self._with_page(f"SA38 RS38M-PROGRAMM={program}", run)


def capture_from_env() -> WebGuiCapture | None:
    cfg = WebGuiConfig.from_env()
    return WebGuiCapture(cfg) if cfg else None


if __name__ == "__main__":
    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--run", nargs=2, metavar=("PROGRAM", "VARIANT"))
    ap.add_argument("--debug", action="store_true", help="save a screenshot of every step to reports/webgui_debug")
    ap.add_argument("--show", action="store_true", help="visible browser instead of headless")
    args = ap.parse_args()
    cfg = WebGuiConfig.from_env()
    if cfg is None:
        raise SystemExit("set SAP_WEBGUI_URL, SAP_WEBGUI_USER and SAP_WEBGUI_PASSWORD in .env")
    out = ROOT / "reports"
    cap = WebGuiCapture(cfg, headless=not args.show, debug_dir=out / "webgui_debug" if args.debug else None)
    if args.run:
        prog, var = (x.upper() for x in args.run)
        print(asyncio.run(cap.capture_run(prog, var, out / f"webgui_{prog}_{var}.png")))
    else:
        print(asyncio.run(cap.probe(out / "webgui_probe.png")))
