from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from skills_sync.cli import main
raise SystemExit(main())
