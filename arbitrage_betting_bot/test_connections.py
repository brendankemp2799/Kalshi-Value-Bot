"""
Connection test — run this before starting the bot to verify all API keys work.

Usage:
    python test_connections.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def test_odds_api() -> tuple[bool, str]:
    try:
        import requests
        import config
        url = f"{config.ODDS_API_BASE_URL}/sports"
        resp = requests.get(url, params={"apiKey": config.ODDS_API_KEY}, timeout=10)
        if resp.status_code == 401:
            return False, "Invalid API key (401)"
        resp.raise_for_status()
        sports = resp.json()
        remaining = resp.headers.get("x-requests-remaining", "?")
        return True, f"{len(sports)} sports available  |  {remaining} requests remaining"
    except Exception as e:
        return False, str(e)


def test_kalshi_read() -> tuple[bool, str]:
    """Test that the API key + private key can authenticate and read market data."""
    try:
        import requests
        import config
        from data.kalshi_auth import auth_headers

        url = f"{config.KALSHI_API_BASE_URL}/markets"
        headers = auth_headers("GET", url)
        resp = requests.get(url, params={"limit": 1, "status": "open"}, headers=headers, timeout=10)
        if resp.status_code == 401:
            return False, "Auth failed (401) — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH"
        if resp.status_code == 403:
            return False, "Permission denied (403) — key may not have trading access"
        resp.raise_for_status()
        return True, "Authentication successful — market data readable"
    except FileNotFoundError as e:
        return False, f"Private key file not found: {e}"
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def test_kalshi_balance() -> tuple[bool, str]:
    """Test that the key can access account/portfolio data (required for trading)."""
    try:
        import requests
        import config
        from data.kalshi_auth import auth_headers

        url = f"{config.KALSHI_API_BASE_URL}/portfolio/balance"
        headers = auth_headers("GET", url)
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 401:
            return False, "Auth failed (401)"
        if resp.status_code == 403:
            return False, "Permission denied (403) — enable trading access on this API key"
        resp.raise_for_status()
        data = resp.json()
        balance_cents = data.get("balance", 0)
        balance = balance_cents / 100
        return True, f"Account balance: ${balance:,.2f}"
    except Exception as e:
        return False, str(e)


def test_private_key_file() -> tuple[bool, str]:
    """Check that the private key file exists and is a valid PEM."""
    try:
        import config
        from pathlib import Path
        path = Path(config.KALSHI_PRIVATE_KEY_PATH).expanduser()
        if not config.KALSHI_PRIVATE_KEY_PATH:
            return False, "KALSHI_PRIVATE_KEY_PATH not set in .env"
        if not path.exists():
            return False, f"File not found: {path}"
        content = path.read_text()
        if "BEGIN" not in content or "PRIVATE KEY" not in content:
            return False, "File exists but does not look like a PEM private key"
        from cryptography.hazmat.primitives import serialization
        serialization.load_pem_private_key(path.read_bytes(), password=None)
        return True, f"Valid PEM key loaded from {path}"
    except Exception as e:
        return False, str(e)


def main():
    console.print()
    console.rule("[bold cyan]API Connection Test[/bold cyan]")
    console.print()

    tests = [
        ("The Odds API",         test_odds_api),
        ("Kalshi private key",   test_private_key_file),
        ("Kalshi auth (read)",   test_kalshi_read),
        ("Kalshi balance",       test_kalshi_balance),
    ]

    table = Table(box=box.ROUNDED, show_header=True, padding=(0, 2))
    table.add_column("Test", style="bold cyan", min_width=22)
    table.add_column("Status", min_width=8)
    table.add_column("Detail", style="dim")

    all_passed = True
    for name, fn in tests:
        passed, detail = fn()
        status = "[bold green]PASS[/bold green]" if passed else "[bold red]FAIL[/bold red]"
        table.add_row(name, status, detail)
        if not passed:
            all_passed = False

    console.print(table)
    console.print()

    if all_passed:
        console.print("[bold green]All checks passed — ready to run the bot.[/bold green]")
    else:
        console.print("[bold red]One or more checks failed — fix the issues above before running the bot.[/bold red]")

    console.print()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
