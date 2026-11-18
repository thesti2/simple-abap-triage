"""Send a test alert to the running backend, like xMatters would.

  uv run python scripts/send_alert.py div0            # tests/fixtures/alert_div0.json
  uv run python scripts/send_alert.py rfc --new-id    # unique alert_id so it is not a duplicate
  uv run python scripts/send_alert.py data --url https://<CLOUD_RUN_URL>
"""

import argparse
import json
import time
from pathlib import Path

import httpx

from app.config import get_settings

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("name", choices=["div0", "data", "rfc"])
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--new-id", action="store_true", help="append a timestamp to alert_id")
    a = p.parse_args()

    payload = json.loads((FIXTURES / f"alert_{a.name}.json").read_text(encoding="utf-8"))
    if a.new_id:
        payload["alert_id"] += f"-{int(time.time())}"
    r = httpx.post(f"{a.url}/webhooks/xmatters", json=payload,
                   headers={"X-Triage-Token": get_settings().xmatters_webhook_token}, timeout=30)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
