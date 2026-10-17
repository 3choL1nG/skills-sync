"""允许 `python -m skills_sync ...`。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
