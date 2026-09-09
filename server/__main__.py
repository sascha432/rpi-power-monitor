"""Allow running the server with ``python -m server``."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
