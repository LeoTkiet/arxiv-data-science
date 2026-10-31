assert RUN_BENCHMARK, 'Đổi RUN_BENCHMARK=True khi đã kiểm tra cấu hình.'
assert globals().get('SOURCE_READY'), 'Chạy thành công cell cài đặt và kiểm thử source trước.'
if not AUTO_RESUME and OUTPUT.exists() and any(OUTPUT.iterdir()):
    raise ValueError('Fresh run cần output rỗng; chọn output mới hoặc bật resume.')
import uuid
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
RESULTS = RESULTS_ROOT / f'run_{stamp}_{uuid.uuid4().hex}'
if RESULTS.exists():
    raise FileExistsError('Thư mục kết quả đã tồn tại; giữ nguyên dữ liệu và chạy lại cell.')
env = dict(os.environ)
env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(SOURCE / 'src'), env.get('PYTHONPATH')]))
command = [sys.executable, '-m', 'arxiv_pipeline.reporting.milestone1.benchmark',
           '--output', str(OUTPUT), '--results', str(RESULTS), '--', *cli_args]
print('Command:', command)
completed = subprocess.run(command, cwd=str(SOURCE), env=env)
print('Pipeline exit:', completed.returncode, 'Results:', RESULTS)
if not (RESULTS / 'benchmark.json').is_file():
    raise RuntimeError('Benchmark chưa tạo kết quả; xem lỗi cell trước khi audit/đóng gói.')
if completed.returncode:
    for name in ['pipeline.log', 'statistics_error.json']:
        path = RESULTS / name
        if path.exists():
            print(name, path.read_text(encoding='utf-8', errors='replace')[-2000:])
