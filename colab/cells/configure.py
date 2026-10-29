# @title Chọn phạm vi chạy
MEMBER_ROLE = 'TV2' # @param ['TEST_SMOKE_1_BAI', 'TV1', 'TV2', 'TV3', 'TV4', 'TV5', 'ALL_GROUP_25K']
SAVE_TO_DRIVE = True # @param {type:'boolean'}
AUTO_RESUME = True # @param {type:'boolean'}
RUN_BENCHMARK = False # @param {type:'boolean'}
# Chỉ đặt khi đã tổng hợp corpus/checkpoints của cả nhóm vào một output chung.
GROUP_OUTPUT = None

import datetime, json
assignment = json.loads((SOURCE / 'configs/ms1_group6_ranges.json').read_text(encoding='utf-8'))
RANGES_MAP = {
    'TEST_SMOKE_1_BAI': {'range': '2508.12872:2508.12872', 'folder': 'smoke_test'},
    **{f'TV{i + 1}': {'range': item['range'], 'folder': f'tv{i + 1}'}
       for i, item in enumerate(assignment['ranges'])},
    'ALL_GROUP_25K': {'folder': 'all_group6'},
}
cfg = RANGES_MAP[MEMBER_ROLE]
selected_ranges = assignment['ranges'] if MEMBER_ROLE == 'ALL_GROUP_25K' else [{'range': cfg['range']}]
from arxiv_pipeline.main import expand_range
paper_count = len({identifier for item in selected_ranges for identifier in expand_range(item['range'])})
scope_assignment = {'ranges': selected_ranges, 'assigned_input_count': paper_count,
                    'scope': MEMBER_ROLE, 'note': 'ID lookup inputs, not a count of existing publications'}
cli_args = [arg for item in selected_ranges for arg in ['--range', item['range']]]
if AUTO_RESUME:
    cli_args.append('--resume')
base_root = Path('/content/drive/MyDrive/arxiv_ms1_data') if SAVE_TO_DRIVE else Path('/content/ms1-data')
OUTPUT = base_root / cfg['folder'] / 'data'
RESULTS_ROOT = base_root / cfg['folder'] / 'artifacts'
RESULTS = None  # Được tạo tên mới mỗi lần chạy cell benchmark.
OUTPUT.mkdir(parents=True, exist_ok=True)
print({'member': MEMBER_ROLE, 'assigned_inputs': paper_count, 'output': str(OUTPUT),
       'results_parent': str(RESULTS_ROOT), 'resume': AUTO_RESUME,
       'free_disk_bytes': shutil.disk_usage(OUTPUT).free})
print('Đối chiếu tên/MSSV với Excel để xác nhận thứ tự TV1–TV5 trước khi chạy.')
