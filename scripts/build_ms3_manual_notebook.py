"""Build a deterministic, source-only T3-04 Colab notebook."""
import base64
import hashlib
import io
import json
from pathlib import Path
import textwrap
import zipfile

# CLI baseline. The runtime digest, not this baseline alone, identifies all code.
CODE_BASE_COMMIT = 'f60a13d227a3f0890c892685e811deb4a49c3539'
RUNTIME_FILES = [
    'src/arxiv_pipeline/__init__.py',
    'src/arxiv_pipeline/common/__init__.py',
    'src/arxiv_pipeline/common/schemas.py',
    'src/arxiv_pipeline/common/utils.py',
    'src/arxiv_pipeline/matching/__init__.py',
    'src/arxiv_pipeline/matching/contracts.py',
    'src/arxiv_pipeline/matching/labeling.py',
    'src/arxiv_pipeline/matching/annotation_workbook.py',
    'src/arxiv_pipeline/matching/manual_workflow.py',
    'src/arxiv_pipeline/matching/manual_colab.py',
    'configs/ms3_manual_assignments.json',
]

SETUP = '''\
# @title 1. Chuẩn bị code và kết nối Drive
MOUNT_DRIVE = True # @param {type:'boolean'}
RUNTIME_PARENT = '/content/t3-04-runtime'

import base64, hashlib, io, json, stat, sys, zipfile
from pathlib import Path, PurePosixPath
RUNTIME_B64 = __PAYLOAD__
RUNTIME_SHA256 = __DIGEST__
CODE_BASE_COMMIT = __BASE_COMMIT__
if sys.version_info < (3, 10):
    raise RuntimeError('Cần Python 3.10 trở lên.')
payload = base64.b64decode(RUNTIME_B64, validate=True)
if hashlib.sha256(payload).hexdigest() != RUNTIME_SHA256:
    raise RuntimeError('Runtime không khớp SHA-256; dùng notebook gốc.')
runtime = Path(RUNTIME_PARENT) / RUNTIME_SHA256[:16]
loaded = sys.modules.get('arxiv_pipeline')
if loaded and not Path(loaded.__file__).resolve().is_relative_to(runtime.resolve()):
    raise RuntimeError('Đã nạp code arxiv_pipeline khác. Restart session rồi chạy lại.')
runtime.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        target = runtime.joinpath(*relative.parts)
        if relative.is_absolute() or '..' in relative.parts or '\\\\' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
            raise RuntimeError('Runtime có đường dẫn không hợp lệ.')
        if not target.resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError('Runtime path vượt ngoài root.')
        data = archive.read(info)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
manifest = json.loads((runtime/'runtime_manifest.json').read_text())
for name, expected in manifest['files'].items():
    if hashlib.sha256((runtime/name).read_bytes()).hexdigest() != expected:
        raise RuntimeError('Runtime file hash sai: ' + name)
sys.path.insert(0, str(runtime/'src'))
if MOUNT_DRIVE:
    from google.colab import drive
    drive.mount('/content/drive')
print('Runtime SHA-256:', RUNTIME_SHA256)
print('CLI base commit:', CODE_BASE_COMMIT)
print('Runtime chỉ chứa code/config công khai; không có corpus hoặc nhãn.')
'''

CONFIGURE = '''\
# @title 2. Chọn dữ liệu, XLSX và thư mục kết quả
INPUT_MODE = 'staged_zip' # @param ['staged_zip', 'prepared_folder']
INPUT_SOURCE = '/content/drive/MyDrive/NMKHDL_MS3_T3-04_Gan_nhan_2026-11-22/T3-04_inputs.zip' # @param {type:'string'}
WORKBOOK_FOLDER = '/content/drive/MyDrive/NMKHDL_MS3_T3-04_Gan_nhan_2026-11-22/03_DA_REVIEW' # @param {type:'string'}
FILE_PATTERN = 'annotation_{member}_reviewed.xlsx' # @param {type:'string'}
MEMBERS = 'tv1,tv2,tv3,tv4,tv5' # @param {type:'string'}
MODE = 'reviewed' # @param ['reviewed', 'progress']
ORIGIN = 'human' # @param ['human', 'ai_assisted']
OUTPUT_ROOT = '/content/drive/MyDrive/NMKHDL_MS3_T3-04_Gan_nhan_2026-11-22/04_KET_QUA_COLAB' # @param {type:'string'}
ASSIGNMENTS_FILE = '' # @param {type:'string'}
RUN_ID = '' # @param {type:'string'}
LOCAL_ROOT = '/content/t3-04-runs'

from arxiv_pipeline.matching.manual_colab import RunConfig
config = RunConfig(
    input_source=Path(INPUT_SOURCE),
    assignments_file=Path(ASSIGNMENTS_FILE) if ASSIGNMENTS_FILE else runtime/'configs/ms3_manual_assignments.json',
    workbook_folder=Path(WORKBOOK_FOLDER), output_root=Path(OUTPUT_ROOT),
    local_root=Path(LOCAL_ROOT), input_mode=INPUT_MODE,
    members=tuple(v.strip().lower() for v in MEMBERS.split(',') if v.strip()),
    file_pattern=FILE_PATTERN, origin=ORIGIN, mode=MODE, run_id=RUN_ID,
)
print('Input:', config.input_source)
print('Thành viên:', ', '.join(config.members))
for member in config.members:
    print(member, '→', config.workbook_folder/config.file_pattern.format(member=member))
print('Lưu kết quả:', config.output_root)
print('Chế độ:', config.mode, '| nguồn nhãn:', config.origin)
'''

AUDIT = '''\
# @title 3. Chạy prepare → collect → audit trên ổ local
from arxiv_pipeline.matching.manual_colab import run_audit
result = run_audit(config, runtime_sha256=RUNTIME_SHA256, code_base_commit=CODE_BASE_COMMIT)
audit = json.loads((result.root/'collected/manual_audit.json').read_text())
print('\\nTiến độ theo thành viên:')
for member, values in audit['members'].items():
    print(member, '| bài đạt:', values['qualifying_publications'], '| accepted entries:', values['accepted_entries'], '| quota:', values['meets_quota'])
print('Lỗi:', len(audit['errors']), '| chưa gán:', audit['counts']['unlabeled'])
print('ready_for_handoff:', audit['ready_for_handoff'])
print('Run local:', result.root)
'''

PUBLISH = '''\
# @title 4. Lưu kết quả và bằng chứng lên Drive
from arxiv_pipeline.matching.manual_colab import publish_run
published = publish_run(result, config.output_root)
receipt = json.loads((published/'published_manifest.json').read_text())
print('Thư mục kết quả:', published)
print('Gói ZIP:', receipt['archive'])
print('SHA-256:', receipt['archive_sha256'])
if receipt['handoff_ready']:
    print('ĐẠT AUDIT NHÃN: gửi labels, audit, manifest và ZIP cho TV5 review bàn giao.')
else:
    print('BÁO CÁO TIẾN ĐỘ: chưa bàn giao nghiệm thu; sửa lỗi/thiếu nhãn rồi chạy lượt mới.')
print('Lượt chạy cũ được giữ nguyên. Chạy lại bước 3 tạo run mới nếu RUN_ID để trống.')
'''


def _cell(kind, source, identity):
    cell = dict(cell_type=kind, id=identity, metadata={}, source=textwrap.dedent(source).splitlines(keepends=True))
    if kind == 'code':
        cell.update(execution_count=None, outputs=[])
    return cell


def build(root: Path, destination: Path) -> str:
    files = {name: (root/name).read_text(encoding='utf-8').encode('utf-8') for name in RUNTIME_FILES}
    manifest = dict(code_base_commit=CODE_BASE_COMMIT,
        files={name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
    files['runtime_manifest.json'] = json.dumps(manifest, sort_keys=True).encode('utf-8')
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980,1,1,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    raw = payload.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    setup = SETUP.replace('__PAYLOAD__', repr(base64.b64encode(raw).decode('ascii'))).replace('__DIGEST__', repr(digest)).replace('__BASE_COMMIT__', repr(CODE_BASE_COMMIT))
    notebook = dict(nbformat=4, nbformat_minor=5,
        metadata=dict(kernelspec=dict(display_name='Python 3', language='python', name='python3'),
            language_info=dict(name='python'), colab=dict(name='MS3_Manual_Labeling_CPU.ipynb'),
            t3_04=dict(runtime_sha256=digest, code_base_commit=CODE_BASE_COMMIT)),
        cells=[
            _cell('markdown', '''\
                # T3-04 · Thu nhãn XLSX và audit trên Colab CPU

                Chạy lần lượt 4 bước. Code được đóng gói từ nhánh `feature/m3-tv3-manual-labeling`,
                gọi lại CLI hiện có, không cần clone nhánh chưa push hoặc cài thư viện Excel.
                Runtime được ghim bằng SHA-256; base commit chỉ là baseline CLI, không thay cho runtime digest.
                Không cần GPU và không huấn luyện model trong notebook này.

                Giữ nguyên phiếu gốc và hướng dẫn gán nhãn riêng. Input là staged ZIP của 25 bài hoặc prepared snapshot;
                không phải ZIP corpus MS1/MS2 đầy đủ. XLSX dùng sheet Gan_nhan và Candidates.
                ''', 'intro'),
            _cell('code', setup, 'setup'),
            _cell('markdown', '''\
                ## Cấu hình

                Sau khi review: đặt 5 file `annotation_tvN_reviewed.xlsx` vào `03_DA_REVIEW`, dùng
                MEMBERS=tv1,tv2,tv3,tv4,tv5, MODE=reviewed và ORIGIN=human.
                Input `T3-04_inputs.zip` phải có đúng 125 JSON theo layout tvN/paper_key/{bibtex,references,
                ms1_validation,ms2_validation,input_manifest}.json. Hoặc chọn prepared_folder chứa prepared_manifest.json,
                bundles và phiếu gốc đã xuất bằng CLI. ASSIGNMENTS_FILE trống dùng config 25 bài đã chốt.

                Muốn kiểm tra bản TV3 đang pending: MEMBERS=tv3, MODE=progress, ORIGIN=ai_assisted,
                WORKBOOK_FOLDER trỏ tới `02_DA_GAN_NHAN`, FILE_PATTERN=annotation_{member}_annotated_draft.xlsx.
                Không có nhãn pending/uncertain nào được tự chuyển sang accepted. AI draft không phải human-reviewed gold.
                Dùng đường dẫn Drive đã mount, không dùng URL Drive. Tên file phải khớp, không tự chọn file mới nhất.

                RUN_ID để trống sẽ tạo lượt mới. Nếu nhập tên riêng, phải chưa tồn tại ở local và output.
                Không chạy nhiều runtime cùng RUN_ID và không chỉnh input/XLSX trong lúc cell 3 đang đọc.
                ''', 'configuration-help'),
            _cell('code', CONFIGURE, 'configure'),
            _cell('code', AUDIT, 'audit'),
            _cell('markdown', '''\
                ## Đọc kết quả và lưu

                Exit 1 của collect/audit là báo cáo chưa đạt, không bị đổi thành thành công.
                Bài/người thiếu quota, nhãn sai hoặc self-review chặn bàn giao.
                Pending/uncertain không được tính vào quota; vẫn có thể đạt audit nếu số nhãn accepted đủ.
                Exit 2/dữ liệu thiếu làm cell dừng; sửa đường dẫn hoặc dữ liệu rồi chạy lại bước 3 với RUN_ID mới.

                Chỉ có handoff.zip khi MODE=reviewed, ORIGIN=human và audit thật sự ready.
                Các lượt còn lại xuất progress.zip. Bước 4 kiểm lại hash/nhãn trước khi lưu, không ghi đè run cũ.
                ZIP kèm prepared snapshot, XLSX đã đọc, labels, audit, manifest, config và log để tái lập.

                Đây là audit nhãn manual T3-04, chưa phải nghiệm thu MS3 end-to-end. Kiểm tra snapshot đầu vào ở mức
                artifacts staged/validation reports, chưa thay thế TV1 xác minh producer checkpoints/dependencies.
                TV5 vẫn cần review bàn giao; auto pool T3-05, split và training là các bước riêng.
                ''', 'publication-help'),
            _cell('code', PUBLISH, 'publish'),
        ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(notebook, ensure_ascii=False, indent=1)+'\n', encoding='utf-8')
    return digest


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    digest = build(root, root/'colab/MS3_Manual_Labeling_CPU.ipynb')
    print('MS3_Manual_Labeling_CPU.ipynb runtime SHA-256:', digest)
