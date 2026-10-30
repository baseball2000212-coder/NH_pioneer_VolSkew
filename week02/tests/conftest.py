"""테스트 공용: 제출 폴더(상위)를 import 경로에 올린다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
