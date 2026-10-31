# Bàn giao hợp đồng MS2 — phần A của TV1

Ngày chuẩn bị: 29/10/2026. Đây là tài liệu để người dùng chuyển cho nhóm;
chưa gửi thông điệp ra ngoài và chưa có xác nhận TV2–TV5.

## Tài liệu và mã bàn giao

- `docs/MS2_CONTRACTS.md`: audit MS1, schema, adapter envelope, artifact,
  điều kiện ready/empty/error, occurrence sidecar và coverage manifest.
- `src/arxiv_pipeline/common/schemas.py`: ElementRow, HierarchyRow, BibtexRow,
  PARSING_SCHEMA_VERSION. Không đổi schema mặc định MS1.
- `tests/parsing/ms2_contract_examples.py`: ví dụ JSON qua hàm `examples()` và
  corpus giả lập qua `write_corpus_fixture(input_root)`.
- `tests/parsing/test_ms2_contracts.py`: kiểm chứng hợp đồng, ví dụ hai version,
  occurrence lặp và corpus fixture trong thư mục tạm.

Chạy tests từ repo root:

```powershell
python -m pytest tests/parsing/test_ms2_contracts.py -q
python -m pytest -q
```

## Nội dung nhóm cần xác nhận

| Thành viên | Nội dung cần đối chiếu | Trạng thái |
| --- | --- | --- |
| TV1 | Bốn cột hierarchy theo đề, artifacts/điều kiện bàn giao; baseline nội bộ | Đã chuẩn bị; quyết định bốn cột được người dùng chốt |
| TV2 | Đã thống nhất dataclass `ParsedNode` trong schemas.py, envelope `raw_tree`, `temp_id`, `position`, cây empty | Đã xác nhận & đồng bộ |
| TV3 | Envelope `stages.hierarchy`, cleaned_content, đánh lại position sau tách câu | Chờ xác nhận |
| TV4 | Nhánh bibliography đọc resolve; gom versions; ba bảng, occurrences và manifest | Chờ xác nhận |
| TV5 | Validation MS2 riêng, root không có cạnh null, empty coverage, counts trước/sau dedup | Chờ xác nhận |

Baseline có thể chỉnh trước tích hợp nhưng bảng chính giữ schema đề. Thay đổi
phá vỡ hợp đồng cần cập nhật tài liệu, ví dụ/tests và consumers cùng nhau.

## Phạm vi và phần còn lại

Kiểm chứng local: 8 test hợp đồng nằm trong toàn bộ **148 tests và 45 subtests
qua**; `git diff --check` sạch. Bộ test toàn repo chạy với
`--basetemp artifacts/pytest-ms2-<unique-id>` vì thư mục temp mặc định pytest
không truy cập được trong sandbox. Không sửa code MS1 để xử lý lỗi môi trường này.

Review độc lập đã kiểm tra schema/docs/examples. Đã sửa mâu thuẫn `.bib`-only:
empty body không buộc bibtex rỗng; giữ references nếu còn đọc được `.bib`.
Ví dụ cũng có cảnh báo khi không cung cấp source map và lý do version empty.

- Chưa làm Resolver, Orchestrator, CLI hay validator MS2 (phần B/C).
- Chưa đối chiếu dữ liệu thật của lần crawl đang chạy, chưa nghiệm thu MS1/MS2.
- Dùng fixture tạm để phát triển ngay; khi có mẫu MS1 hoàn tất, cần nhận cả
  discovery, checkpoint download/extract và sidecar bằng chứng liên quan.
- Chưa có đường dẫn snapshot MS1 thực tế được bàn giao; không tự chọn dữ liệu
  trong output đang chạy làm nguồn nghiệm thu.
- Nhánh bàn giao phần A: `feature/m2-tv1-contracts`, tách từ `origin/main`
  (`c90e79c`) theo yêu cầu người dùng commit/push; không ghép vào PR MS1.
- Checklist theo dõi nằm tại `docs/checklists/MS2_TV1_CHECKLIST.md`.
