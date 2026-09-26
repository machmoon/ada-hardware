"""``python -m alexabot`` -- serve Ada's voice tools over Streamable HTTP.

Exit codes follow the CLI convention: 0 success, 2 a configuration problem.
"""

from .app import main

if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
