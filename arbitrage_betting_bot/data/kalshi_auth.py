"""
Kalshi RSA request signing.

Kalshi's trading API requires every authenticated request to be signed with
your RSA private key. The signature proves the request came from you and
hasn't been tampered with.

How it works:
  1. Take the current time in milliseconds, the HTTP method, and the URL path.
  2. Concatenate them: "{timestamp_ms}{METHOD}{/path?query}"
  3. Sign that string with your RSA private key (PKCS#1 v1.5, SHA-256).
  4. Base64-encode the signature and send it in the KALSHI-ACCESS-SIGNATURE header.

Required headers for every authenticated request:
  KALSHI-ACCESS-KEY          — your API key UUID
  KALSHI-ACCESS-TIMESTAMP    — millisecond timestamp used in the signature
  KALSHI-ACCESS-SIGNATURE    — base64(RSA_sign(private_key, message))

Your private key is stored as a PEM file on disk. Set KALSHI_PRIVATE_KEY_PATH
in .env to point to it.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_private_key_cache = None


def _load_private_key():
    """Load and cache the RSA private key from disk."""
    global _private_key_cache
    if _private_key_cache is not None:
        return _private_key_cache

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise RuntimeError(
            "cryptography package not installed. Run: pip install cryptography"
        )

    path = config.KALSHI_PRIVATE_KEY_PATH
    if not path:
        raise RuntimeError(
            "KALSHI_PRIVATE_KEY_PATH not set in .env — "
            "point it to your RSA private key PEM file"
        )

    pem_path = Path(path).expanduser()
    if not pem_path.exists():
        raise RuntimeError(f"Kalshi private key not found at: {pem_path}")

    with open(pem_path, "rb") as f:
        _private_key_cache = serialization.load_pem_private_key(
            f.read(), password=None
        )

    logger.debug("Kalshi private key loaded from %s", pem_path)
    return _private_key_cache


def auth_headers(method: str, url: str) -> dict[str, str]:
    """
    Build Kalshi auth headers for a request.

    Args:
        method: HTTP method in any case ("GET", "POST", etc.)
        url:    Full request URL — only the path + query string is used for signing.

    Returns:
        Dict of headers to merge into the request.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise RuntimeError(
            "cryptography package not installed. Run: pip install cryptography"
        )

    private_key = _load_private_key()

    timestamp_ms = str(int(time.time() * 1000))
    # Kalshi signs: timestamp + METHOD + /path  (query string excluded)
    path = urlparse(url).path
    message = timestamp_ms + method.upper() + path

    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
