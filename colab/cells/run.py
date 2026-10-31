assert RUN_BENCHMARK, 'Đổi RUN_BENCHMARK=True khi đã kiểm tra cấu hình.'
assert globals().get('SOURCE_READY'), 'Chạy thành công cell cài đặt và kiểm thử source trước.'
if not AUTO_RESUME and OUTPUT.exists() and any(OUTPUT.iterdir()):
    raise ValueError('Fresh run cần output rỗng; chọn output mới hoặc bật resume.')
import uuid, time
from types import SimpleNamespace
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
RESULTS = RESULTS_ROOT / f'run_{stamp}_{uuid.uuid4().hex}'
if RESULTS.exists():
    raise FileExistsError('Thư mục kết quả đã tồn tại; giữ nguyên dữ liệu và chạy lại cell.')
env = dict(os.environ)
env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(SOURCE / 'src'), env.get('PYTHONPATH')]))

if globals().get('FAST_BATCH_PREFETCH'):
    print('⚡ [Batch Pre-fetch] Bắt đầu gom lô Metadata arXiv và S2 References...')
    batch_args = [arg for item in effective_ranges for arg in ['--range', item['range']]]
    try:
        subprocess.run([sys.executable, '-m', 'arxiv_pipeline.crawling.discovery_batch',
                        '--output', str(OUTPUT), *batch_args], cwd=str(SOURCE), env=env)
    except Exception as exc:
        print('⚠️ Batch discovery notice:', exc)
    try:
        subprocess.run([sys.executable, '-m', 'arxiv_pipeline.crawling.s2.batch',
                        '--output', str(OUTPUT), *batch_args], cwd=str(SOURCE), env=env)
    except Exception as exc:
        print('⚠️ Batch S2 notice:', exc)

command = [sys.executable, '-m', 'arxiv_pipeline.reporting.milestone1.benchmark',
           '--output', str(OUTPUT), '--results', str(RESULTS), '--', *cli_args]
print('Command:', command)

if hasattr(subprocess, 'Popen'):
    start_time = time.time()
    proc = subprocess.Popen(command, cwd=str(SOURCE), env=env)
    timings_path = RESULTS / 'timings.jsonl'
    target_count = len({identifier for item in effective_ranges for identifier in expand_range(item['range'])})
    while proc.poll() is None:
        time.sleep(1.5)
        if timings_path.is_file():
            try:
                lines = timings_path.read_text(encoding='utf-8', errors='replace').splitlines()
                papers = [l for l in lines if '"kind": "paper"' in l or '"kind":"paper"' in l]
                finished = len(papers)
                elapsed = time.time() - start_time
                pct = (finished / target_count * 100) if target_count else 0
                speed = (elapsed / finished) if finished > 0 else 0
                eta_sec = (target_count - finished) * speed if target_count >= finished else 0
                eta_str = f"{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
                last_line = lines[-1] if lines else ''
                last_id = ''
                if '"paper_id":' in last_line:
                    last_id = last_line.split('"paper_id":')[1].split(',')[0].strip(' "')
                print(f"\r🚀 Tiến độ: {finished}/{target_count} ({pct:.1f}%) | Bài: {last_id or 'đang xử lý'} | Tốc độ: {speed:.1f}s/bài | ETA: {eta_str}", end='', flush=True)
            except Exception:
                pass
    proc.wait()
    print()
    completed = SimpleNamespace(returncode=proc.returncode)
else:
    completed = subprocess.run(command, cwd=str(SOURCE), env=env)

print('Pipeline exit:', completed.returncode, 'Results:', RESULTS)
if not (RESULTS / 'benchmark.json').is_file():
    raise RuntimeError('Benchmark chưa tạo kết quả; xem lỗi cell trước khi audit/đóng gói.')
if completed.returncode:
    for name in ['pipeline.log', 'statistics_error.json']:
        path = RESULTS / name
        if path.exists():
            print(name, path.read_text(encoding='utf-8', errors='replace')[-2000:])
