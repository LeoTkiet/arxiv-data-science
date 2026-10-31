# @title Chọn phạm vi chạy
MEMBER_ROLE = 'TV2' # @param ['TEST_SMOKE_1_BAI', 'TV1', 'TV2', 'TV3', 'TV4', 'TV5', 'ALL_GROUP_25K']
SAVE_TO_DRIVE = True # @param {type:'boolean'}
AUTO_RESUME = True # @param {type:'boolean'}
FAST_BATCH_PREFETCH = True # @param {type:'boolean'}
RESUME_START_ID = 'AUTO' # @param ['AUTO', 'START'] {allow-input: true}
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

base_root = Path('/content/drive/MyDrive/arxiv_ms1_data') if SAVE_TO_DRIVE else Path('/content/ms1-data')
OUTPUT = base_root / cfg['folder'] / 'data'
RESULTS_ROOT = base_root / cfg['folder'] / 'artifacts'
RESULTS = None  # Được tạo tên mới mỗi lần chạy cell benchmark.
OUTPUT.mkdir(parents=True, exist_ok=True)

# Smart Fast Resume: Căn cứ DUY NHẤT vào file validation đã hoàn tất (không tính folder rỗng)
effective_ranges = list(selected_ranges)
if AUTO_RESUME and MEMBER_ROLE not in {'ALL_GROUP_25K', 'TEST_SMOKE_1_BAI'} and RESUME_START_ID != 'START':
    full_ids = expand_range(cfg['range'])
    month_prefix = full_ids[0].split('.')[0] + '.'
    val_dir = OUTPUT / 'validation'
    done_ids = []
    if val_dir.is_dir():
        for p in val_dir.glob(f'{month_prefix}*.json'):
            if p.is_file() and p.stat().st_size > 0 and p.stem in full_ids:
                done_ids.append(p.stem)
    if done_ids:
        max_done = max(done_ids)
        max_idx = full_ids.index(max_done)
        target_idx = max(0, max_idx - 5)
        new_start = full_ids[target_idx] if RESUME_START_ID == 'AUTO' else RESUME_START_ID
        if new_start in full_ids and full_ids.index(new_start) > 0:
            new_range = f"{new_start}:{full_ids[-1]}"
            effective_ranges = [{'range': new_range}]
            print(f"🔄 [Smart Fast Resume]: Đã thẩm định hoàn tất đến {max_done}. Tự động lùi 5 bài để tiếp tục từ {new_range} (bỏ qua {target_idx} bài cũ đã thẩm định).")

cli_args = [arg for item in effective_ranges for arg in ['--range', item['range']]]
if AUTO_RESUME:
    cli_args.append('--resume')
print({'member': MEMBER_ROLE, 'assigned_inputs': paper_count, 'output': str(OUTPUT),
       'results_parent': str(RESULTS_ROOT), 'resume': AUTO_RESUME,
       'free_disk_bytes': shutil.disk_usage(OUTPUT).free})
print('Đối chiếu tên/MSSV với Excel để xác nhận thứ tự TV1–TV5 trước khi chạy.')
