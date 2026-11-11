# arxiv-data-science

## TV5 — MS1 benchmark và báo cáo

Hướng dẫn đầy đủ, công thức và giới hạn phép đo tại [docs/TV5_MS1_GUIDE.md](docs/TV5_MS1_GUIDE.md). Module mới nằm trong `src/arxiv_pipeline/reporting/milestone1/`; không thay đổi phần MS2.

```powershell
python -m pip install -e ".[reporting,dev]"
python -m arxiv_pipeline.reporting.milestone1.benchmark --output data/ms1 --results artifacts/ms1-run -- --ids 2508.12872
python -m arxiv_pipeline.reporting.milestone1.export --run artifacts/ms1-run --book ../arxiv-project-report --name local-smoke
python -m pytest -q
```

Input assignment nhóm 6 ở `configs/ms1_group6_ranges.json` (25.000 ID lookup). Benchmark cuối cần Colab CPU-only; smoke local/demo không thay thế số đo nghiệm thu. Mỗi results mới, nằm ngoài output; `--resume` phải ghi nhãn điều kiện chạy. Repository Jupyter Book là thư mục/repo riêng `arxiv-project-report`.

Notebook chạy cho nhóm: [colab/MS1_Benchmark_CPU.ipynb](colab/MS1_Benchmark_CPU.ipynb). Hướng dẫn chọn phạm vi, resume, audit cá nhân/toàn nhóm và kiểm thử: [colab/README.md](colab/README.md). Notebook cố định source commit đã kiểm chứng, giữ kết quả các lượt trước và kiểm run ID/hash trước khi xuất lại thống kê. Workflow build/deploy báo cáo vẫn ở repo Jupyter Book.

Phần bàn giao TV1: cấu trúc project MS1–MS3, Discovery mọi version, hợp đồng dữ liệu v1 và CLI điều phối có checkpoint/resume. Python **3.10+**, runtime TV1 chỉ dùng standard library.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
arxiv-pipeline --ids 2301.00001 2301.00002 --output data --stage discovery
```

Có thể chạy ngay từ checkout, không cần cài package:

```powershell
python main.py --ids 2301.00001 --output data
python main.py --input ids.txt --range 2301.00001:2301.00003 --output data --resume
python scripts/run_pipeline.py --ids hep-th/9901001 --output data
python main.py --ids 2301.00001 --output data --force
```

`ids.txt` dùng UTF-8, mỗi dòng một ID, cho phép dòng trống và comment `#`. `--ids` nhận ID phân cách bằng khoảng trắng hoặc dấu phẩy, URL abs/pdf, tiền tố `arXiv:` và version cụ thể. Range là khoảng đóng trong cùng tháng, tối đa 10.000 ID. Đầu vào version cụ thể vẫn phát hiện toàn bộ version của paper; version không tồn tại được ghi lỗi riêng. ID không hợp lệ không làm dừng các ID còn lại.

`--resume` dùng lại discovery hoàn chỉnh và thử lại các input thất bại. `--force` cập nhật snapshot latest và chạy lại các adapter; không dùng cùng `--resume`. Mặc định không có resume thì chạy mới. `--timeout 30`, `--retries 3`, `--request-delay 3` là mặc định; retries không gồm lần đầu, delay tối thiểu 3 giây. Mỗi output root chỉ có một process ghi tại một thời điểm.

Đầu ra chính là `manifests/discovery.json`, `manifests/stages.json`, `summary.json`, `state/` và `logs/pipeline.log` bên trong output root. Record `success` có đầy đủ v1..vn, metadata ngày từng version và đường dẫn source/corpus cố định. `invalid_id`, `not_found`, `api_error`, `data_error` được tách biệt. Không tải source bằng lệnh discovery.

Hợp đồng cho **TV2–TV5**, schema, đường dẫn và giao diện adapter nằm ở [docs/CONTRACTS.md](docs/CONTRACTS.md). Đây là baseline TV1 để nhóm review; chưa có xác nhận của các thành viên. Các module chưa bàn giao có hàm `run(...)` ném `NotImplementedError` rõ ràng.

```powershell
python main.py --input ids.txt --output data --stage crawl --resume
```

Lệnh crawl kết nối Discovery → download → extract, sau đó metadata → references → validate. Khi adapter chưa được bàn giao, manifest ghi `not_implemented`/`skipped`, exit 1. Không báo thành công toàn pipeline khi mới có Discovery. MS2/MS3 và Parquet là khung chờ triển khai.

Kiểm thử offline (không gọi arXiv, không cần tải dependencies):

```powershell
python -m unittest discover -s tests -v
```

Tests bao gồm HTTP retry, lỗi Atom/data, ID cũ/mới, mọi version, CLI và resume sau lỗi/gián đoạn, tích hợp adapter giả lập. Live arXiv cần Internet; fixtures không chứng minh API đang khả dụng. Exit: 0 hoàn tất, 1 có lỗi/chưa bàn giao, 2 tham số sai, 130 bị ngắt. `no_source`, `not_indexed`, `no_data` là outcome riêng, không dùng để tính success rate của source.

## Reference crawler (Semantic Scholar)

Stage `references` lấy danh sách tham khảo của mỗi paper từ Semantic Scholar. Code ở `src/arxiv_pipeline/crawling/s2/`, adapter là `crawling/references.py`. 

**Đặt key.** Tạo `.env` ở thư mục gốc repo (đã gitignore), theo mẫu `.env.example`:

```text
S2_API_KEY=your_semantic_scholar_key
```

Không có key vẫn chạy, nhưng dùng pool chung (giãn cách 5 giây, dễ bị 429). Biến `S2_API_KEY` đã export trong shell được ưu tiên hơn `.env`. Kiểm tra: đầu log có dòng `S2 API key loaded (...xxxx)`.

**Chạy** Stage `references` chạy cùng pipeline, mỗi paper một request:

```powershell
python main.py --input ids.txt --output data --stage crawl --resume
```

Nhanh hơn khi có nhiều paper: lấy references trước theo batch (100 paper mỗi request), rồi pipeline bỏ qua stage này nhờ `--resume`. Chạy lần lượt, không chạy song song:

```powershell
python -m arxiv_pipeline.crawling.s2.batch --input ids.txt --output data
python main.py --input ids.txt --output data --stage crawl --resume
```

Chạy lại cùng lệnh: paper đã xong được bỏ qua, paper lỗi được thử lại. Test: `python -m unittest tests.crawling.test_s2_references -v`.

**Schema đầu ra** (trong `--output`):

| File | Nội dung |
| --- | --- |
| `references/<paper_key>.json` | Mảng reference, gồm cả reference không có arXiv ID |
| `references/<paper_key>.provenance.json` | `source`, `retrieved_at`, `status`, `s2_paper_id`, `venue`, `reference_count`, `paginated` |
| `state/stages/references/<paper_key>/paper.json` | `StageResult` dùng để resume |
| `state/s2_progress.jsonl` | Mỗi lần thử một dòng: `arxiv_id`, `status`, `n_refs`, `paginated`, `seconds` |

Một reference:

```json
{
  "arxiv_id": "2508.12872",
  "ref_arxiv_id": null,
  "ref_title": "Example title",
  "ref_authors": ["A. Author", "B. Author"],
  "ref_submission_date": "2024-06-01",
  "ref_s2_id": "dd1d030eb34bfef20bc56c37a3d2cd8f17cbc778",
  "missing_fields": ["ref_arxiv_id"]
}
```

- `ref_arxiv_id` chỉ lấy từ trường `ArXiv` của Semantic Scholar, không suy ra từ DOI. Trường scalar thiếu là `null`, danh sách thiếu là `[]`, và tên trường được ghi vào `missing_fields`.
- `ref_submission_date` là `YYYY-MM-DD`, hoặc chỉ `YYYY` khi S2 chỉ có năm.
- Reference trùng (cùng S2 ID, hoặc cùng arXiv ID khi không có S2 ID) chỉ giữ một lần. Mục rỗng bị bỏ.
- Status: `success` (có reference), `no_data` (S2 có paper nhưng không có reference), `not_indexed` (S2 không biết paper), `api_error` (hết retry hoặc HTTP 400, không ghi file, thử lại ở lần chạy sau).


## Metadata

Stage `metadata` (`crawling/metadata.py`) không gọi mạng: nó kiểm tra record Discovery, chuẩn hoá và ghi `metadata/<paper_key>.json`. Cấp paper có đúng các cột bảng `papers` (`arxiv_id`, `title`, `authors`, `submission_date` từ v1, `revised_dates` từ v2..vn, `venue` từ `journal_ref`, có thể `null`) cùng `abstract`, `categories`, `doi`, `source`, `retrieved_at`. Mỗi phần tử của `versions[]` có metadata riêng của version đó (`title`, `authors`, `venue`, `submission_date`, `updated_date`). Giá trị thiếu là `null`/`[]` và được ghi trong `missing_fields`; không suy đoán. Record không hợp lệ cho `data_error`, không ghi file. Test: `python -m unittest tests.crawling.test_metadata -v`.

## Validator (TV5)

Stage `validate` đã được triển khai trong `src/arxiv_pipeline/export/validation.py` và tự chạy cuối mỗi paper khi dùng `--stage crawl`:

```powershell
python main.py --input ids.txt --output data --stage crawl --resume
python -m unittest discover -s tests -p test_validation.py -v
```

Validator đọc checkpoint tại `state/stages/`, không phụ thuộc manifest tổng hợp vốn chỉ được ghi sau khi toàn pipeline kết thúc. Kiểm tra gồm danh sách version, artifact/đường dẫn, source/checksum SHA-256 nếu sidecar có ghi, corpus `.tex`/`.bib`, metadata so với Discovery và schema/reference trùng. Cho phép sidecar `extract_metadata.json`. Không sửa corpus hay gọi API.

Kết quả là `validation/<paper_key>.json`: `valid`, `counts`, `outcomes`, `errors`, `warnings`, `duplicate_files`. Mỗi lỗi có paper ID, version nếu áp dụng, đường dẫn, mã lỗi và mô tả. Dữ liệu không hợp lệ trả `StageResult.status=data_error`; JSON chẩn đoán vẫn được lưu. `success` của validator chỉ có nghĩa kết quả nhất quán: `no_source`, `no_data`, `not_indexed` vẫn được giữ riêng; không được dùng `validate=success` để tính tỷ lệ tải source thành công. Source trùng nội dung là cảnh báo vì các version có thể dùng chung nội dung; references trùng định danh là lỗi.

`--resume` có thể dùng lại validation đã thành công; nó không buộc kiểm tra lại nội dung. `--force` chạy lại cả pipeline và có thể tải lại dữ liệu. Để chỉ kiểm tra lại dữ liệu đã có, gọi adapter trực tiếp từ Python, tại repo root:

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path('src').resolve()))
from arxiv_pipeline.export.validation import run

output = Path('data')
records = json.loads((output / 'manifests/discovery.json').read_text(encoding='utf-8'))
for record in records:
    if record['status'] == 'success':
        result = run(record, output)
        print(result.paper_id, result.status, result.artifacts)
```

Sau một lần chạy bị gián đoạn, có thể đọc record từ `state/discovery/*.json` thay manifest. Gọi trực tiếp chỉ cập nhật báo cáo validation, không cập nhật checkpoint validate hay summary của orchestrator. Chạy kiểm tra sau khi tiến trình ghi corpus đã dừng. Benchmark tài nguyên và báo cáo Jupyter Book là các phần riêng, chưa nằm trong validator.

Quy trình nhóm: tạo issue có người phụ trách, input/output và tiêu chí nghiệm thu; làm trên nhánh `feature/...` theo kế hoạch nhóm, PR có hướng dẫn test và ít nhất một reviewer, không commit trực tiếp main. Việc merge nhánh thành viên, tạo repository báo cáo và benchmark Colab thực hiện khi có module/quyền truy cập tương ứng.

## Milestone 2 — điều phối parsing offline

Parsing đọc một snapshot MS1 đã ghi xong, không gọi lại arXiv/Semantic Scholar.
Input MS1 và output MS2 phải là hai thư mục tách rời; không chạy nhiều CLI ghi
cùng output root và không dùng corpus đang có tiến trình khác cập nhật.

```powershell
python main.py --stage parse --ms1-input artifacts/ms1-snapshot --output artifacts/ms2 --ids 2310.12345 --resume
```

Bỏ `--ids` để chọn mọi record trong snapshot. Có thể dùng `--input ids.txt` hoặc
`--range` để lọc paper như MS1; ID chọn parsing không có hậu tố version.
`--versions 1 2` giới hạn các version được xử lý. Nếu chưa chọn đủ mọi version
MS1 của một paper, pipeline ghi incomplete và không công bố bảng cấp paper.
`--force` chạy lại các stage trong phạm vi đã chọn; không xóa kết quả paper khác
hoặc ghi vào MS1. `--resume` và `--force` không dùng đồng thời.

Luồng theo version: Resolver → Hierarchy → Normalization, cùng nhánh Resolver →
Bibliography. Sau khi đủ các version: Deduplication → Validation MS2. Bibliography
vẫn nhận version không có TeX để xử lý `.bib` nếu còn; version empty không bị
loại khỏi mẫu số coverage. Discovery `not_found`/`invalid_id` được ghi riêng ở
`excluded_inputs`, không tạo publication giả.

Sáu module thật đã tích hợp trên `main`, gồm validator TV5 sau PR #19.
Module thiếu vẫn trả `not_implemented` và làm lượt chạy incomplete. Tests gồm
các ca điều phối bằng mock và các ca chạy đủ module thật bằng fixture offline.

Đầu ra điều phối chính:

- `parsing_summary.json`: complete, paper_results, stage_statuses, input_errors.
- `manifests/parsing.json`, `manifests/stages.json`: kết quả của lượt chọn hiện tại.
- `state/stages/<stage>/<paper_key>/<N|paper>.json`: StageResult schema 2.0.
- File `.dependencies.json` đi kèm: fingerprint đầu vào/mã module/checkpoint và
  hash artifact để kiểm tra resume; artifact thiếu, hỏng hoặc thay đổi phải chạy lại.
- `state/parsing/<paper_key>.json`: tình trạng đầy đủ của từng paper. Bảng cuối
  chỉ được xem là hoàn tất khi trạng thái pipeline này có complete=true.

Version mới hoặc nội dung đầu vào thay đổi làm mất hiệu lực export/validation
cấp paper. Nếu snapshot đổi giữa quá trình xử lý, pipeline không công bố complete.
Export cũ bị đánh complete=false khi upstream hiện tại chưa đủ; dữ liệu không bị
xóa. Validator MS1 ở `export/validation.py` giữ nguyên; adapter TV5 MS2 được nối
qua `reporting/milestone2/validation.py`, stage `validate_parse`.

Exit code parsing: `0` khi mọi publication đã chọn hoàn tất hoặc là empty hợp
lệ, `1` khi lỗi/chưa bàn giao/chưa đủ version, `2` khi tham số CLI sai, `130` khi
bị ngắt. Chi tiết envelope và artifact xem [hợp đồng MS2](docs/MS2_CONTRACTS.md).

```powershell
python -m pytest tests/pipeline/test_parse.py tests/pipeline/test_cli.py -q
```

Notebook [MS2 Integration CPU](colab/MS2_Integration_CPU.ipynb) chạy module thật,
đo tài nguyên, kiểm tra resume và ghi bằng chứng theo từng thành viên. Chọn mẫu
bằng danh sách ID; chỉ chạy mọi record khi đã có snapshot đầy đủ. Đối chiếu đúng
ID được giao trước khi xác nhận toàn phạm vi, không chỉ so số lượng. Xem kết quả
và giới hạn nghiệm thu trong [bàn giao MS2](docs/MS2_HANDOFF.md).

## Milestone 3: data contracts 3.0

Contract nhóm đã chốt nằm ở [docs/MS3_CONTRACTS.md](docs/MS3_CONTRACTS.md).
Các module M3 dùng records/validators trong `arxiv_pipeline.matching.contracts`;
`BibtexInput` dùng lại `BibtexRow` M2. Checkpoint M3 phải truyền tường minh
`schema_version=MATCHING_SCHEMA_VERSION`; default M1/M2 giữ nguyên.

[Compatibility fixtures](tests/fixtures/matching/README.md) có exact, ambiguous,
unmatched, empty references và split mock cho nhóm 5 người. Không dùng dữ liệu
synthetic làm bằng chứng nghiệm thu. Task này cung cấp schema/validators/fixtures;
loader snapshot và pipeline matching end-to-end chưa triển khai.

```powershell
python -m pytest tests/matching/test_contracts.py -q
```
