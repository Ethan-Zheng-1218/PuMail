"""运行 Python 回归测试，隔离 HOME 和临时目录，不读取真实邮箱数据。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    scratch = root / '.diagnostics'
    scratch.mkdir(exist_ok=True)
    original_expand = os.path.expanduser
    original_temp = tempfile.tempdir
    with tempfile.TemporaryDirectory(dir=scratch, ignore_cleanup_errors=True) as home:
        tempfile.tempdir = home
        try:
            with patch('os.path.expanduser', side_effect=lambda p: home if p == '~' else original_expand(p)):
                suite = unittest.defaultTestLoader.discover(str(root), pattern='test_*.py')
                result = unittest.TextTestRunner(verbosity=1).run(suite)
        finally:
            tempfile.tempdir = original_temp
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
