"""Monkey-patch Pyrogram 2.0.106 Session.start() to fix the infinite
DC reconnection loop.

Root cause: Session.start() has ``while True:`` with no backoff or retry
limit for (OSError, RPCError).  When a DC keeps dropping connections
(rate-limit, transient network issue, etc.) this creates a tight loop
that floods reconnections and never recovers.

Fix: limited retries with exponential backoff + longer START_TIMEOUT.
"""

import asyncio
import logging

from pyrogram import raw
from pyrogram.connection import Connection
from pyrogram.errors import AuthKeyDuplicated, RPCError
from pyrogram.raw.all import layer
from pyrogram.session.session import Session

log = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────
MAX_SESSION_RETRIES = 20        # finite retry budget per session
INITIAL_BACKOFF     = 3         # seconds before first retry
MAX_BACKOFF         = 60        # cap
SESSION_TIMEOUT     = 10        # seconds (original: 2)

# Give each Ping / InitConnection more time so transient slowness
# doesn't instantly trigger a retry.
Session.START_TIMEOUT = SESSION_TIMEOUT


# ── Patched Session.start() ──────────────────────────────────────
async def _patched_start(self):
    """Session.start() with exponential backoff and retry limit."""
    backoff = INITIAL_BACKOFF

    for attempt in range(1, MAX_SESSION_RETRIES + 1):
        self.connection = Connection(
            self.dc_id,
            self.test_mode,
            self.client.ipv6,
            self.client.proxy,
            self.is_media,
        )

        try:
            await self.connection.connect()

            self.recv_task = self.loop.create_task(self.recv_worker())

            await self.send(
                raw.functions.Ping(ping_id=0),
                timeout=self.START_TIMEOUT,
            )

            if not self.is_cdn:
                await self.send(
                    raw.functions.InvokeWithLayer(
                        layer=layer,
                        query=raw.functions.InitConnection(
                            api_id=await self.client.storage.api_id(),
                            app_version=self.client.app_version,
                            device_model=self.client.device_model,
                            system_version=self.client.system_version,
                            system_lang_code=self.client.lang_code,
                            lang_code=self.client.lang_code,
                            lang_pack="",
                            query=raw.functions.help.GetConfig(),
                        ),
                    ),
                    timeout=self.START_TIMEOUT,
                )

            self.ping_task = self.loop.create_task(self.ping_worker())

            log.info("Session initialized: Layer %s", layer)
            log.info(
                "Device: %s - %s",
                self.client.device_model,
                self.client.app_version,
            )
            log.info(
                "System: %s (%s)",
                self.client.system_version,
                self.client.lang_code,
            )
            break                       # success → exit loop

        except AuthKeyDuplicated as e:
            await self.stop()
            raise e

        except (OSError, RPCError, TimeoutError) as e:
            await self.stop()
            if attempt < MAX_SESSION_RETRIES:
                log.warning(
                    "DC%s attempt %d/%d failed (%s), retrying in %ds…",
                    self.dc_id, attempt, MAX_SESSION_RETRIES, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                raise ConnectionError(
                    f"DC{self.dc_id} unreachable after "
                    f"{MAX_SESSION_RETRIES} attempts"
                ) from e

        except Exception as e:
            await self.stop()
            raise e
    else:
        # for-else: loop exhausted without break
        raise ConnectionError(
            f"DC{self.dc_id} unreachable after {MAX_SESSION_RETRIES} attempts"
        )

    self.is_started.set()
    log.info("Session started")


Session.start = _patched_start
log.info("Pyrogram Session.start() patched (backoff=%ds–%ds, retries=%d, timeout=%ds)",
         INITIAL_BACKOFF, MAX_BACKOFF, MAX_SESSION_RETRIES, SESSION_TIMEOUT)
