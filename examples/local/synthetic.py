"""
Synthetic "API" for local runs — loaded by the `python` connector
(see duckduck.local.json). Each function is a table; its parameters get
push-down exactly like a real connector's, so the same SQL keeps working
when the config points at the real source instead.
"""

import random
from datetime import datetime, timedelta
from typing import Optional

from duckduck.pushdown import require_like


def tables(rows: int = 1000, seed: int = 7):
    rng = random.Random(seed)
    now = datetime.utcnow().replace(microsecond=0)
    assets = [
        {
            "hostname": f"{rng.choice(['web', 'db', 'app'])}-{i:04d}",
            "ip": f"10.{i // 65536}.{i // 256 % 256}.{i % 256}",
            "os": rng.choice(["linux", "windows"]),
            "risk": rng.randint(0, 100),
            "last_seen": (now - timedelta(hours=rng.randint(0, 24 * 30))).isoformat(),
        }
        for i in range(rows)
    ]

    def assets_table(
        os: Optional[str] = None,
        hostname_ilike: Optional[str] = None,
        risk_gte: Optional[int] = None,
        limit: Optional[int] = None,
    ):
        """Synthetic asset inventory (os =, hostname LIKE, risk >= push down)."""
        out = assets
        if os is not None:
            out = [a for a in out if a["os"] == os]
        if hostname_ilike is not None:
            like = require_like(hostname_ilike)
            text = like.text.lower()
            match = {"contains": lambda h: text in h, "startswith": lambda h: h.startswith(text),
                     "endswith": lambda h: h.endswith(text), "equals": lambda h: h == text}[like.kind]
            out = [a for a in out if match(a["hostname"].lower())]
        if risk_gte is not None:
            out = [a for a in out if a["risk"] >= risk_gte]
        return out[:limit] if limit else out

    return {"assets": assets_table}
