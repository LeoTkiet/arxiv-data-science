"""Package current public Python source into one portable recovery notebook."""
import base64
import hashlib
import io
import json
from pathlib import Path
import textwrap
import zipfile

REQUIREMENTS = 'requests==2.34.2\npython-dotenv==1.2.4\nbibtexparser==1.4.4\npsutil==7.2.2\n'

SETUP = '''\
# @title 1. Chọn thành viên, chuẩn bị dữ liệu và kiểm kê
MEMBER = 'TV1' # @param ['TV1', 'TV2', 'TV3', 'TV4', 'TV5']
SOURCE_DATA = 'AUTO' # @param {type:'string'}
RECOVERY_FOLDER = 'AUTO' # @param {type:'string'}

import base64, hashlib, importlib.metadata, io, json, os, stat, subprocess, sys, time, zipfile
from pathlib import Path, PurePosixPath
RUNTIME_B64 = __PAYLOAD__
RUNTIME_SHA256 = __HASH__
RUNTIME_BASE = '/content/ms1-recovery-runtime'
LOCAL_BASE = '/content/ms1-recovery'
if sys.version_info < (3, 10):
    raise RuntimeError('Cần Python 3.10 trở lên; chọn runtime CPU mới.')
runtime = Path(RUNTIME_BASE) / RUNTIME_SHA256[:16]
payload = base64.b64decode(RUNTIME_B64, validate=True)
if hashlib.sha256(payload).hexdigest() != RUNTIME_SHA256:
    raise RuntimeError('Gói runtime không khớp SHA-256. Dùng lại notebook gốc.')
runtime.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    for info in archive.infolist():
        relative = PurePosixPath(info.filename)
        if relative.is_absolute() or '..' in relative.parts or '\\\\' in info.filename or stat.S_ISLNK(info.external_attr >> 16):
            raise RuntimeError('Gói runtime chứa đường dẫn không hợp lệ.')
        target = runtime / info.filename
        if not target.resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError('Đường dẫn runtime vượt ngoài thư mục cài đặt.')
        data = archive.read(info)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest():
            target.write_bytes(data)
requirements = (runtime/'requirements-recovery.txt').read_text().splitlines()
missing = []
for requirement in requirements:
    package, version = requirement.split('==')
    try:
        if importlib.metadata.version(package) != version: missing.append(requirement)
    except importlib.metadata.PackageNotFoundError:
        missing.append(requirement)
if missing:
    print('Đang cài thư viện cho runtime phục hồi...', flush=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', *missing], check=True)
loaded = sys.modules.get('arxiv_pipeline')
if loaded and not Path(loaded.__file__).resolve().is_relative_to(runtime.resolve()):
    raise RuntimeError('Runtime đã nạp arxiv_pipeline khác. Restart session rồi chạy lại notebook này.')
sys.path.insert(0, str(runtime/'src'))
from arxiv_pipeline.pipeline.ms1_recovery_colab import RecoverySession, select_assignment
source = Path(SOURCE_DATA if SOURCE_DATA != 'AUTO' else f'/content/drive/MyDrive/arxiv_ms1_data/{MEMBER.lower()}/data')
persistent = Path(RECOVERY_FOLDER if RECOVERY_FOLDER != 'AUTO' else f'/content/drive/MyDrive/arxiv_ms1_recovery/{MEMBER.lower()}')
if any(str(path).startswith('/content/drive/') for path in (source, persistent)):
    from google.colab import drive
    drive.mount('/content/drive')
assignment = select_assignment(MEMBER, runtime/'configs/ms1_group6_ranges.json')
print(f"{MEMBER}: {assignment['ranges'][0]['range']} | {assignment['assigned_input_count']} ID được giao", flush=True)
print(f'Đọc dữ liệu gốc: {source}\\nThư mục kết quả riêng: {persistent}', flush=True)
last_output = [0.0]
def progress(event):
    now = time.monotonic()
    final = event.get('done') == event.get('total') and event.get('total', 0) >= 64
    if now - last_output[0] < 5 and not final:
        return
    last_output[0] = now
    kind = event['kind']
    if kind == 'phase':
        print(event['message'], flush=True)
    elif kind == 'copy':
        seconds = max(event.get('seconds', 0), 0.01)
        rate = event.get('bytes', 0) / seconds / 1024**2
        print(f"Chép về local: {event['done']}/{event['total']} file | dùng lại {event.get('reused',0)} file | {rate:.1f} MiB/s", flush=True)
    elif kind == 'inventory':
        print(f"Kiểm kê local: {event['done']}/{event['total']} ID | {event['paper_id']}", flush=True)
    elif kind == 'publish':
        print(f"Lưu kết quả: {event['done']}/{event['total']} file", flush=True)
    elif kind == 'stage':
        print(f"{event['paper_id']} v{event['version']} | {event['stage']}: {event['status']}", flush=True)
session = RecoverySession(MEMBER, source, persistent, Path(LOCAL_BASE)/MEMBER.lower(), assignment, progress=progress)
plan = session.inspect()
print('Kết quả kiểm kê:', plan['summary'])
print('Danh sách chi tiết:', session.evidence/'inventory.csv')
pending = [dict(paper_id=paper['paper_id'], **action)
           for paper in plan['papers'] for action in paper['actions'] if action['decision'] != 'keep']
for action in pending[:20]:
    print(f"{action['paper_id']} v{action['version']} | {action['stage']}: {action['decision']} | {action['reason']}")
if len(pending) > 20:
    print(f'Còn {len(pending)-20} hành động trong inventory.csv.')
print('Discovery cần xem xét:', plan['diagnostics'])
if session.applied:
    print('Đã nhận lại lượt phục hồi đã lưu. Có thể chạy bước 3 để kiểm định và xuất bàn giao.')
else:
    print('Chưa chạy phục hồi. Xem danh sách rồi chọn chế độ ở bước 2.')
'''

REPAIR = '''\
# @title 2. Phục hồi theo danh sách đã kiểm kê
RUN_MODE = 'Chỉ kiểm kê' # @param ['Chỉ kiểm kê', 'Phục hồi local', 'Phục hồi có tải/API']
modes = {'Chỉ kiểm kê':'inspect', 'Phục hồi local':'local', 'Phục hồi có tải/API':'network'}
if 'session' not in globals():
    raise RuntimeError('Chạy bước 1 trước.')
recovery_result = session.repair(modes[RUN_MODE])
if recovery_result.get('dry_run'):
    print('Lượt này chỉ kiểm kê; không chạy thêm phục hồi hoặc gọi API.')
    if session.applied:
        print('Bước 3 sẽ kiểm định và xuất kết quả từ lượt phục hồi đã lưu trước đó.')
else:
    print('Đã kết thúc lượt phục hồi. Đạt mọi stage trong phạm vi:', recovery_result['complete'])
    print('Phần còn lại:', recovery_result['remaining'])
    print('ID not_found:', recovery_result['excluded'])
    print('Chạy bước 3 để kiểm định và xuất bàn giao; chưa dùng output làm snapshot MS2.')
'''

FINISH = '''\
# @title 3. Kiểm định và xuất gói bàn giao
if 'session' not in globals():
    raise RuntimeError('Chạy bước 1 trước.')
handoff = session.finish()
if handoff.get('mode') == 'inventory_only':
    print('Đã xuất ZIP kiểm kê; chưa xuất bản dữ liệu phục hồi.')
else:
    print('Thư mục data phục hồi:', session.persistent/'data')
    print('Đủ điều kiện bàn giao MS2 trong phạm vi đã kiểm tra:', handoff['handoff_ready'])
    print('ID còn cần xử lý:', handoff['unresolved_paper_ids'])
    print('Chẩn đoán cần xem xét:', handoff['diagnostics'])
    print('Snapshot ID:', handoff['snapshot_id'])
print('ZIP bằng chứng (không chứa raw/corpus):', handoff['evidence_zip'])
'''


def build(root, destination):
    root, destination = Path(root).resolve(), Path(destination)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
        paths = sorted((root/'src/arxiv_pipeline').rglob('*.py'))
        paths += [root/'configs/ms1_group6_ranges.json']
        for path in paths:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(2026,11,18,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            # Normalize source newlines so the package hash is portable.
            archive.writestr(info, path.read_text(encoding='utf-8').replace('\r\n','\n').encode('utf-8'))
        info = zipfile.ZipInfo('requirements-recovery.txt', date_time=(2026,11,18,0,0,0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, REQUIREMENTS)
    data = payload.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    setup = SETUP.replace('__PAYLOAD__', repr(base64.b64encode(data).decode('ascii'))).replace('__HASH__', repr(digest))
    def cell(kind, text, name):
        row = dict(cell_type=kind, id=name, metadata={}, source=textwrap.dedent(text).splitlines(keepends=True))
        if kind == 'code':
            row.update(execution_count=None, outputs=[])
            row['metadata'] = dict(cellView='form')
        return row
    notebook = dict(nbformat=4, nbformat_minor=5,
        metadata=dict(kernelspec=dict(display_name='Python 3', language='python', name='python3'),
                      language_info=dict(name='python', version='3.12'),
                      colab=dict(name='MS1_Selective_Recovery_CPU.ipynb', provenance=[]),
                      ms1_recovery=dict(runtime_sha256=digest, distribution='embedded-local-source', network_default=False)),
        cells=[
            cell('markdown', '''\
                # Phục hồi chọn lọc dữ liệu MS1 (CPU)

                Mỗi TV dùng đúng dữ liệu và dải ID đã được giao. Notebook giữ nguyên thư mục gốc,
                chỉ phục hồi phần thiếu/lỗi trong một bản sao riêng. Chọn **CPU** trong Colab.
                Bản vá đã nhúng sẵn, không cần clone GitHub hoặc tìm file runtime khác.

                ## 1. Chuẩn bị và kiểm kê

                - **MEMBER:** chọn TV1–TV5; dải 5.000 ID lấy từ config phân công của nhóm.
                - **SOURCE_DATA:** đường dẫn đến thư mục `data/` gốc trên Drive, không phải URL chia sẻ
                  hoặc ZIP báo cáo. `AUTO` dùng `MyDrive/arxiv_ms1_data/tvN/data` như notebook MS1 cũ.
                - **RECOVERY_FOLDER:** thư mục kết quả riêng. `AUTO` dùng `MyDrive/arxiv_ms1_recovery/tvN`.

                Dừng notebook MS1 cũ và mọi tiến trình ghi vào nguồn trước khi chạy.
                Bước này chép dữ liệu về ổ Colab bằng tối đa 32 luồng, rồi hash/giải nén đối chứng trên local.
                Cần đủ chỗ cho một bản dữ liệu đầy đủ trên Colab và một bản trên Drive khi bật phục hồi.
                Lần đầu vẫn phải đọc nguồn để xác minh; notebook hiển thị tiến độ chép và kiểm kê.
                ''', 'intro'),
            cell('code', setup, 'inventory'),
            cell('markdown', '''\
                ## 2. Phục hồi phần đã xác định

                Xem `inventory.csv` trong thư mục evidence local của bước 1 trước khi chọn chế độ.

                | Chế độ | Công việc |
                | --- | --- |
                | Chỉ kiểm kê (mặc định) | Không sửa dữ liệu, không gọi API. |
                | Phục hồi local | Extract từ raw tốt; sửa metadata/QA. Download và references cần mạng vẫn để lại trong danh sách. |
                | Phục hồi có tải/API | Cho phép tải đúng version lỗi và lấy references thiếu/lỗi; giữ nguyên raw/corpus tốt. |

                404 hoặc kết quả không đủ bằng chứng được để lại để xem xét. Không đổi lỗi thành empty.
                Khi bật phục hồi lần đầu, notebook lưu một bản nền vào thư mục kết quả riêng để resume.
                Sau đó chỉ lưu artifact/checkpoint của stage vừa chạy. Không mở hai runtime ghi cùng output.
                ''', 'repair-help'),
            cell('code', REPAIR, 'repair'),
            cell('markdown', '''\
                ## 3. Kiểm định và bàn giao

                Sau phục hồi, bước này kiểm tra lại assignment, cập nhật QA, discovery/stages/summary/receipt
                và xuất `data/` cùng ZIP bằng chứng. ZIP chứa audit, danh sách đã sửa, lỗi còn lại và snapshot;
                **không chứa toàn bộ raw/corpus**. Chỉ dùng bản phục hồi cho MS2 khi receipt ghi
                `handoff_ready=true`. Đây là nghiệm thu phạm vi một TV, chưa phải toàn nhóm.

                Nếu chưa từng chạy phục hồi trên output này, bước này chỉ xuất gói kiểm kê.
                Nếu đã có lượt phục hồi được lưu, notebook nhận lại nhật ký để xuất bàn giao,
                kể cả sau khi chạy lại bước 1 hoặc mở runtime mới; không cần tải/extract lại chỉ để xuất báo cáo.
                ''', 'handoff-help'),
            cell('code', FINISH, 'handoff'),
            cell('markdown', '''\
                ## Tiếp tục sau khi bị ngắt

                Dừng hẳn cell đang chạy. Giữ nguyên MEMBER, SOURCE_DATA và RECOVERY_FOLDER rồi chạy lại bước 1–3.
                Trong cùng runtime, notebook dùng bản local hiện có. Sau khi Colab mất runtime, notebook chép lại
                bản phục hồi đã lưu trên Drive về local rồi xác minh checkpoint; stage tốt không bị gọi lại.
                Việc chép lại file sau khi mất ổ Colab vẫn cần thời gian. Gián đoạn lúc chép nền có thể phải
                chép lại phần chưa được xác nhận; chưa chạy phục hồi cho đến khi bản nền lưu xong.

                Checkpoint được lưu sau từng stage. Nếu runtime mất ngay trong lúc một stage đang ghi/lưu,
                stage đó có thể cần chạy lại; không cam kết resume đến từng byte hay bỏ qua kiểm tra tính toàn vẹn.
                Muốn đổi dữ liệu nguồn hoặc thử lượt độc lập, chọn RECOVERY_FOLDER mới; không ghi đè phiên khác.
                Nếu còn lỗi, gửi ZIP bằng chứng và link thư mục kết quả cho TV1. Giai đoạn pilot MS2 làm sau.
                ''', 'resume-help')])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(notebook, ensure_ascii=False, indent=1)+'\n', encoding='utf-8')
    return digest


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    target = root/'colab/MS1_Selective_Recovery_CPU.ipynb'
    digest = build(root, target)
    print(f'{target.name}: runtime SHA-256 {digest}')
