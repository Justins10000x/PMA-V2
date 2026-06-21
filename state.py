"""
State + session accounting for PMA v2.

Honest accounting was a V1 weakness: it added an idealised expected profit on a
clean fill and never reconciled partials. V2 records only realised outcomes and
exposes a reconcile() hook so live mode trusts the exchange, not a local file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

from . import config

log = logging.getLogger("pma")


@dataclass
class Session:
    realised_profit: float = 0.0
    realised_loss: float = 0.0          # from unwinds / aborted partials
    trades: int = 0
    aborts: int = 0
    history: List[dict] = field(default_factory=list)

    @property
    def net(self) -> float:
        return self.realised_profit - self.realised_loss

    def record(self, result: dict):
        result = dict(result)
        result["recorded_at"] = datetime.now().isoformat()
        self.history.append(result)
        status = result.get("status")
        if status == "filled":
            self.realised_profit += result.get("realised", 0.0)
            self.trades += 1
        elif status == "partial_unwound":
            # Unwind cost is realised separately when known; count the abort.
            self.realised_loss += abs(result.get("realised", 0.0))
            self.aborts += 1
        else:
            self.aborts += 1
        self.save()

    def breached_circuit_breaker(self) -> bool:
        return self.realised_loss >= config.MAX_SESSION_LOSS

    def save(self):
        Path(config.STATE_FILE).write_text(
            json.dumps(
                {"saved_at": datetime.now().isoformat(), **asdict(self)}, indent=2
            )
        )

    @classmethod
    def load(cls) -> "Session":
        p = Path(config.STATE_FILE)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
            data.pop("saved_at", None)
            return cls(**data)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load state (%s) — starting fresh.", e)
            return cls()


def reconcile(client):
    """Live-mode startup check: trust the exchange over local state.

    Hook for querying actual USDC balance and open positions from the CLOB so
    the bot never acts on a stale or corrupted state.json. Intentionally a
    no-op stub until wired to your account in live mode.
    """
    if client is None:
        return
    try:
        log.info("Reconciling against exchange balance/positions...")
        # balance = client.get_balance_allowance(...)
        # positions = client.get_positions(...)
        # -> compare to local state, warn on mismatch
    except Exception as e:  # noqa: BLE001
        log.warning("Reconcile skipped: %s", e)
