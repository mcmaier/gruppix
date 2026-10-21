"""Docker HEALTHCHECK: one readable line instead of a traceback.

Exit 0 = healthy, 1 = unhealthy. Usage: python -m gruppix.healthcheck
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = f"http://localhost:{os.environ.get('HTTP_PORT', '8080')}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = {}
        print("FEHLER: " + ("; ".join(body.get("problems") or []) or f"HTTP {exc.code}"))
        return 1
    except (OSError, ValueError) as exc:
        print(f"FEHLER: Worker antwortet nicht ({type(exc).__name__})")
        return 1
    warnings = body.get("warnings") or []
    print("OK" + (" – Warnung: " + "; ".join(warnings) if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
