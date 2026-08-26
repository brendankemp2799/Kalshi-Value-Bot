"""dashboard_server.py must not define anything AFTER `if __name__ == "__main__":`.

THE BUG (2026-08-25, production). `app.run()` blocks. main.py's `if __name__ ==
"__main__": main()` sat in the MIDDLE of the file, above MM_TEMPLATE and the new
CLV_TEMPLATE. When systemd runs `python3.10 dashboard_server.py` directly (so
__name__ IS "__main__"), the interpreter reaches that guard, calls main(), and
blocks forever inside app.run() -- so every top-level statement BELOW it (both
templates) never executes, and every route that references them raises
NameError the instant it's hit.

Every existing dashboard test uses Flask's test_client() via `import
dashboard_server`, which never triggers this: __name__ is "dashboard_server", not
"__main__", so the guard's body never runs and the whole file executes top to
bottom regardless of where the guard sits. That import-based testing is exactly
why this shipped to production undetected -- it structurally cannot see this
class of bug. This test reads the source directly instead of importing it.
"""
from __future__ import annotations

import os


def _dashboard_source() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "arbitrage_betting_bot",
                        "dashboard_server.py")
    with open(path) as f:
        return f.read()


def test_the_main_guard_is_the_last_top_level_statement():
    src = _dashboard_source()
    guard_pos = src.index('if __name__ == "__main__":')
    after = src[guard_pos:]
    # Nothing meaningful should follow the guard's own two lines (the call to
    # main()) except whitespace.
    after_call = after.split("main()", 1)[1]
    assert after_call.strip() == "", (
        f"code found after the __main__ guard -- it will never execute when this "
        f"file is run as a script (app.run() blocks first): {after_call!r}"
    )


def test_every_template_string_is_defined_before_the_main_guard():
    src = _dashboard_source()
    guard_pos = src.index('if __name__ == "__main__":')
    import re
    for m in re.finditer(r'^([A-Z_]+_TEMPLATE)\s*=\s*"""', src, re.MULTILINE):
        assert m.start() < guard_pos, (
            f"{m.group(1)} is defined AFTER the __main__ guard -- app.run() blocks "
            f"before this line is ever reached when run as a script, so every route "
            f"using it will 500 with NameError in production"
        )


def test_every_route_is_registered_before_the_main_guard():
    """@app.route(...) decorators run at import time (they call app.add_url_rule
    immediately) -- a route defined after the guard would never be registered at
    all when run as a script, not just crash on first use."""
    src = _dashboard_source()
    guard_pos = src.index('if __name__ == "__main__":')
    import re
    for m in re.finditer(r'^@app\.route\(', src, re.MULTILINE):
        assert m.start() < guard_pos, (
            f"a route registered after the __main__ guard at char {m.start()} would "
            f"never be added to the app when run as a script"
        )
