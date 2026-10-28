# arxiv-data-science

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
