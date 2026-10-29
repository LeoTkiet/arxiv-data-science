assert RESULTS is not None and (RESULTS / 'benchmark.json').is_file(), 'Cần có lượt benchmark trước.'
from arxiv_pipeline.reporting.milestone1.benchmark import current_snapshot
from arxiv_pipeline.common.utils import atomic_json

def print_rates(rates):
    for name, rate in rates.items():
        value = rate['value']
        percent = 'không áp dụng/chưa có mẫu số' if value is None else f'{value * 100:.2f}%'
        print(f"{name}: {percent} ({rate['numerator']}/{rate['denominator']})")

report_path = RESULTS / 'benchmark.json'
report = json.loads(report_path.read_text(encoding='utf-8'))
if report.get('statistics_status') == 'current_run':
    before = current_snapshot(OUTPUT, report['run_id'])
    if before != report['snapshot']:
        raise ValueError('Snapshot không khớp benchmark; không xuất lại statistics.')
    subprocess.run([sys.executable, '-m', 'arxiv_pipeline.reporting.milestone1.statistics',
                    '--output', str(OUTPUT), '--save', str(RESULTS / 'statistics-revalidated.json'),
                    '--revalidate'], check=True, cwd=str(SOURCE))
    checked = json.loads((RESULTS / 'statistics-revalidated.json').read_text(encoding='utf-8'))
    if current_snapshot(OUTPUT, report['run_id']) != before:
        raise ValueError('Snapshot thay đổi khi tái kiểm; không xuất lại statistics.')
    checked.update(label=report['label'], run_id=report['run_id'])
    atomic_json(RESULTS / 'statistics.json', checked)
    print_rates(checked['rates'])
else:
    print('Không có snapshot hoàn tất của lượt hiện tại; giữ bằng chứng, không xuất statistics cũ.')
report['environment']['is_colab'] = True
report['environment']['cpu_only_confirmation'] = 'CPU selected manually; NVIDIA GPU probe passed'
atomic_json(report_path, report)

def run_assignment_audit(output, definition, filename, scope):
    import uuid
    attempt = RESULTS / ('audit-' + uuid.uuid4().hex)
    definition_path = attempt / 'inputs.json'
    atomic_json(definition_path, definition)
    saved = attempt / filename
    process = subprocess.run([sys.executable, '-m', 'arxiv_pipeline.reporting.milestone1.audit',
                              '--output', str(output), '--assignment', str(definition_path),
                              '--save', str(saved)], cwd=str(SOURCE))
    if process.returncode not in (0, 1) or not saved.is_file():
        raise RuntimeError(f'Audit {scope} không tạo được báo cáo mới.')
    data = json.loads(saved.read_text(encoding='utf-8'))
    unresolved = len(data['unresolved_discovery_ids'])
    print({'scope': scope, 'assigned': data['assigned_input_count'],
           'resolved': data['discovered_assigned_ids'] - unresolved,
           'unseen': len(data['unseen_ids']), 'unresolved': unresolved,
           'validation_pass': data['validation_pass_papers'],
           'validation_fail': data['validation_fail_papers'], 'complete': data['audit_complete']})
    atomic_json(RESULTS / filename, data)
    return data

audit_data = run_assignment_audit(OUTPUT, scope_assignment, 'assignment-audit.json', MEMBER_ROLE)
if MEMBER_ROLE == 'ALL_GROUP_25K':
    print('Audit trên đã dùng toàn bộ assignment nhóm và output chung.')
elif GROUP_OUTPUT is not None:
    group_output = Path(GROUP_OUTPUT)
    if not group_output.is_dir():
        raise ValueError('GROUP_OUTPUT phải là corpus/checkpoint chung đã tổng hợp.')
    group_audit_data = run_assignment_audit(group_output, assignment, 'group-assignment-audit.json', 'GROUP')
else:
    print('Chưa audit toàn nhóm: cần GROUP_OUTPUT trỏ đến corpus/checkpoint chung đã tổng hợp.')
