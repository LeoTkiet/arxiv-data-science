# Compatibility fixtures M3

Toàn bộ dữ liệu trong `examples.py` là synthetic, seed 42, không dùng mạng và
không phải kết quả gán nhãn hoặc matching thật. Không công bố lên dataset nộp bài.

| Hàm | Nội dung |
| --- | --- |
| `publications()` | Bốn publication: exact, ambiguous, unmatched, references rỗng; ID modern/versioned/legacy và Unicode authors |
| `labels()` | Matched accepted, uncertain pending, unmatched heuristic accepted; query legacy cố ý chưa gán nhãn |
| `partition_rows()` | 25 manual +12 auto; valid/test mỗi tập 5+5, train 15 manual +2 auto |
| `write_compatibility_fixture(root)` | Tạo thư mục mới gồm ms1/references + checkpoints, ms2/tables + manifests, labels, split rows và fixture_manifest có synthetic=true |

Mỗi lần gọi trả dữ liệu mới. Các ca duplicate/conflict/malformed được tạo bằng
bản sao trong test để không làm hỏng fixture dùng chung. Snapshot writer tạo
mock checkpoint của stage references và manifest bảng BibTeX, không giả lập
toàn bộ discovery/validation MS1/MS2. Không dùng nó để nghiệm thu loader thật.
M2 snapshot dùng citing ID canonical; dữ liệu in-memory cố ý có versioned ID
để kiểm tra boundary normalization.

Chạy tại repo root:

```powershell
python -m pytest tests/matching/test_contracts.py -q
```

Trong test, import `from fixtures.matching.examples import publications, labels`.
Đường dẫn `tests` đã nằm trong pytest pythonpath của dự án. Các module nghiệp vụ
import records từ `arxiv_pipeline.matching.contracts`, không import test fixtures
trong production. Các feature giá trị thực, thuật toán matching và MRR thuộc
module của từng TV; contract tests không thay thế evaluation pipeline.
