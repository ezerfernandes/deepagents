"""PKCE (Proof Key for Code Exchange, RFC 7636) helpers.

Ported from `pi-mono/packages/ai/src/utils/oauth/pkce.ts`. We keep the
same 32-byte verifier length so the resulting challenges are
byte-identical to pi-mono's, which makes it easier to share fixtures
between the two test suites if we ever need to.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PKCEPair:
    """A PKCE verifier/challenge pair.

    Attributes:
        verifier: Base64url-encoded 32-byte secret retained by the
            client. Sent verbatim to the token endpoint at exchange
            time.
        challenge: Base64url-encoded SHA-256 of the verifier. Sent in
            the authorization request as `code_challenge`.
    """

    verifier: str
    challenge: str


def _b64url(data: bytes) -> str:
    """Return *data* encoded as base64url without trailing `=` padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> PKCEPair:
    """Return a fresh `PKCEPair` using S256.

    Uses `secrets.token_bytes(32)` for the verifier — the RFC requires
    43-128 base64url characters, and 32 raw bytes encodes to 43 chars,
    matching pi-mono's verifier length exactly.
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = _b64url(verifier_bytes)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PKCEPair(verifier=verifier, challenge=challenge)
