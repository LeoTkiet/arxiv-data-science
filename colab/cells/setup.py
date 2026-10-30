from pathlib import Path
import os, sys, subprocess, json, datetime, shutil

SOURCE_READY = False
SOURCE_URL = 'https://github.com/LeoTkiet/arxiv-data-science.git'
SOURCE_REF = '11ee98b963bb113bd06fc15020130744d9ba3632'
SOURCE = Path('/content/arxiv-data-science')
if not SOURCE.exists():
    subprocess.run(['git', 'clone', SOURCE_URL, str(SOURCE)], check=True)
if subprocess.check_output(['git', '-C', str(SOURCE), 'status', '--porcelain'], text=True).strip():
    raise RuntimeError('Source có thay đổi cục bộ; kiểm tra trước khi đổi commit.')
subprocess.run(['git', '-C', str(SOURCE), 'fetch', 'origin', SOURCE_REF], check=True)
subprocess.run(['git', '-C', str(SOURCE), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
SOURCE_COMMIT = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
if SOURCE_COMMIT != SOURCE_REF:
    raise RuntimeError('Source commit không khớp phiên bản đã kiểm chứng.')
subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-e', str(SOURCE) + '[reporting,dev]'], check=True)
subprocess.run([sys.executable, '-m', 'pytest', 'tests/crawling', 'tests/test_validation.py',
                'tests/test_ms1_reporting.py', '-q'], check=True, cwd=str(SOURCE))
sys.path.insert(0, str(SOURCE / 'src'))
SOURCE_READY = True
print('Source đã kiểm chứng:', SOURCE_COMMIT)
