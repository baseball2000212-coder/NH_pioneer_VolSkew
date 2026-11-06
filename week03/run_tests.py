"""pytest 없이 tests/test_*.py 를 돌린다 (kq150/live/run_tests.py 를 본뜸).
  python run_tests.py            # 전부
  python run_tests.py runtime    # 이름에 runtime 이 든 파일만
지원: tmp_path, capsys(빈 객체), pytest.raises, pytest.approx, pytest.mark.parametrize.
"""
import importlib.util
import inspect
import math
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'tests'))

try:
    import pytest  # noqa: F401
except ImportError:
    shim = types.ModuleType('pytest')

    @contextmanager
    def raises(exc, match=None):
        try:
            yield
        except exc:
            return
        raise AssertionError(f'{exc.__name__} 가 나야 하는데 안 났다')

    class _Approx:
        def __init__(self, expected, rel=None, abs=None):
            self.expected, self.rel, self.abs = expected, rel, abs

        def __eq__(self, other):
            if other is None:
                return False
            tol_abs = self.abs if self.abs is not None else 1e-12
            tol_rel = self.rel if self.rel is not None else 1e-6
            return math.isclose(other, self.expected, rel_tol=tol_rel, abs_tol=tol_abs)

        def __repr__(self):
            return f'approx({self.expected})'

    class _Mark:
        @staticmethod
        def parametrize(names, values):
            def deco(fn):
                fn._params = ([n.strip() for n in names.split(',')] if isinstance(names, str) else list(names), values)
                return fn
            return deco

    shim.raises, shim.mark, shim.approx = raises, _Mark(), _Approx
    sys.modules['pytest'] = shim


def run(filter_text=''):
    files = sorted((HERE / 'tests').glob('test_*.py'))
    passed, failed = 0, []
    for f in files:
        if filter_text and filter_text not in f.name:
            continue
        spec = importlib.util.spec_from_file_location(f'tests_{f.stem}', f)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            failed.append((f'{f.stem} (import)', traceback.format_exc(limit=3)))
            continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith('test_') or fn.__module__ != mod.__name__:
                continue
            cases = [{}]
            if hasattr(fn, '_params'):
                names, values = fn._params
                cases = [dict(zip(names, v if isinstance(v, (tuple, list)) else (v,))) for v in values]
            for i, kw in enumerate(cases):
                label = f'{f.stem}::{name}' + (f'[{i}]' if len(cases) > 1 else '')
                params = inspect.signature(fn).parameters
                try:
                    if 'capsys' in params:
                        kw = dict(kw, capsys=types.SimpleNamespace())
                    if 'tmp_path' in params:
                        with tempfile.TemporaryDirectory() as d:
                            fn(tmp_path=Path(d), **kw)
                    else:
                        fn(**kw)
                    passed += 1
                    print('ok  ', label)
                except Exception:
                    failed.append((label, traceback.format_exc(limit=4)))
    for label, tb in failed:
        print('FAIL', label, '\n', tb)
    print(f'{passed} passed, {len(failed)} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else ''))
