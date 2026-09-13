from __future__ import annotations
import os
import sys
import pytest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

path = sys.argv[1]
rc = pytest.main(["-q", path, "--tb=short"])
os._exit(int(rc))
