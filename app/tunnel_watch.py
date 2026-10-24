"""Keep MAX pointed at the current localhost.run development hostname."""
from __future__ import annotations

import argparse
import asyncio
import logging
import re

import httpx

from app.settings import ROOT, Settings
from app.webhook_manager import read_state, register

log = logging.getLogger(__name__)
HOST = re.compile(r"https://[a-z0-9-]+\.(?:lhr\.life|localhost\.run)(?![a-z0-9.-])")


async def synchronize(settings: Settings):
    path = ROOT / "data/logs/ssh-tunnel.stdout.log"
    if not path.exists():
        return False
    urls = HOST.findall(path.read_text(encoding="utf-8", errors="replace"))
    if not urls:
        return False
    base = urls[-1]
    state = read_state(settings)
    if state["url"] == base + "/webhook/max" and len(state["owned_urls"]) == 1:
        return False
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        response = await client.get(base + "/health/ready")
        if response.status_code != 200 or response.json().get("status") != "ready":
            return False
    await register(settings.model_copy(update={"public_base_url": base}))
    log.info("MAX webhook updated for current tunnel hostname")
    return True


async def run(once=False):
    settings = Settings()
    while True:
        try:
            await synchronize(settings)
        except Exception as exc:
            log.warning("Tunnel synchronization deferred: %s", type(exc).__name__)
        if once:
            return
        await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(run(args.once))
