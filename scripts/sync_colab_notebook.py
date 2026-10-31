"""Embed maintained cell sources; Colab needs only the notebook, no helper downloads."""
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / 'colab/MS1_Benchmark_CPU.ipynb'
doc = json.loads(path.read_text(encoding='utf-8'))
assert len(doc['cells']) == 18, 'Notebook structure changed; check cell mapping before syncing.'
for index, name in {6: 'setup', 10: 'configure', 12: 'run', 14: 'audit'}.items():
    cell = doc['cells'][index]
    assert cell['cell_type'] == 'code'
    cell['source'] = (ROOT / f'colab/cells/{name}.py').read_text(encoding='utf-8').splitlines(keepends=True)
    cell['outputs'] = []
    cell['execution_count'] = None
for cell in doc['cells']:
    cell.setdefault('id', uuid.uuid4().hex[:8])
doc['cells'][5]['source'] = ['## 3. Cài source đã kiểm chứng\n\n',
    'Cố định commit `0f8d83b591c89cf085f344f5fb59af671ab43b81`, kiểm tra source sạch và chạy tests. ',
    'Không fallback âm thầm sang nhánh khác; cell benchmark chỉ chạy khi setup thành công.']
doc['cells'][9]['source'] = ['## 5. Chọn phạm vi\n\n',
    'TV1–TV5 dùng một dải theo thứ tự config; đối chiếu tên/MSSV trong Excel trước khi chạy. ',
    '`AUTO_RESUME` giữ corpus/checkpoint. Mỗi lần chạy benchmark tạo thư mục kết quả mới. ',
    '`GROUP_OUTPUT=None` mặc định; chỉ điền đường dẫn khi đã tổng hợp corpus/checkpoints cả nhóm vào một output chung.']
doc['cells'][13]['source'] = ['## 7. Tái kiểm snapshot và audit\n\n',
    'Kiểm UUID/hash trước và sau revalidate rồi mới xuất lại statistics có cùng run ID/label. ',
    'Audit cá nhân dùng đúng dải đã chọn; smoke dùng 1 ID. ',
    'ALL_GROUP_25K audit toàn assignment trên output chung. Với lượt cá nhân, audit nhóm chỉ chạy nếu có GROUP_OUTPUT; ',
    'không cộng số trong các file summary để giả làm kiểm tra toàn bộ corpus. ',
    'Chỉ audit sau khi các tiến trình ghi dữ liệu đã dừng.']
# Archive names follow the unique run name, preserving earlier runs' ZIP files.
archive_cell = ''.join(doc['cells'][16]['source'])
archive_cell = archive_cell.replace('MS1_RESULTS_{stamp}', 'MS1_RESULTS_{RESULTS.name}')
doc['cells'][16]['source'] = archive_cell.splitlines(keepends=True)
path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
