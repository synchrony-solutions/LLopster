"""Dev helper to mint a signed LLopster license JWT.

This is a developer/operator tool — it is NOT shipped in the image and is NOT
on any runtime path. It requires the **private** signing key, which must live
outside this repo (a secret manager in production; ``.license-signing-key.pem``,
gitignored, in dev). The matching public key is embedded in
``src/agent/license.py`` and verifies what this script signs.

Example — mint a 1-year Business license entitling multi-cluster + the JVM pack:

    .venv/bin/python scripts/sign_license.py \\
        --key .license-signing-key.pem \\
        --tier business \\
        --license-id acme-prod-001 \\
        --feature multi_cluster \\
        --feature pack:jvm-pack \\
        --clusters 5 \\
        --days 365

Prints the JWT to stdout. Hand it to an operator, who sets it as
``LLOPSTER_LICENSE_KEY`` (Secret) or pastes it into the dashboard Settings page.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import jwt

# Mirror the algorithm the engine verifies with (see src/agent/license.py).
_ALGORITHM = "EdDSA"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mint a signed LLopster license JWT.")
    p.add_argument("--key", required=True, help="Path to the Ed25519 private key PEM.")
    p.add_argument("--tier", default="business", choices=["community", "business", "enterprise"])
    p.add_argument("--license-id", required=True, help="Customer/license identifier (-> sub).")
    p.add_argument(
        "--feature", action="append", default=[],
        help="A granted feature; repeatable. Use 'pack:<id>' to entitle a pack.",
    )
    p.add_argument("--clusters", type=int, default=None, help="Licensed cluster count (optional).")
    p.add_argument("--days", type=int, default=365, help="Validity window in days (default 365).")
    args = p.parse_args(argv)

    with open(args.key, "rb") as f:
        private_key = f.read()

    now = datetime.now(timezone.utc)
    claims = {
        "tier": args.tier,
        "features": args.feature,
        "license_id": args.license_id,
        "sub": args.license_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=args.days)).timestamp()),
    }
    if args.clusters is not None:
        claims["clusters"] = args.clusters

    token = jwt.encode(claims, private_key, algorithm=_ALGORITHM)
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
