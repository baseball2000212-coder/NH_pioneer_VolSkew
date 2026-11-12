"""테스트 공용: submission/ 을 import 경로에 올린다(상위 폴더의 모듈을 평평하게 import)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
