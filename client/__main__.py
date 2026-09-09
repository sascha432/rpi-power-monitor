"""Allow running the GUI client with ``python -m client``."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
