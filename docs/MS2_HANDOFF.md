# Bàn giao tích hợp MS2 — TV1 và các thành viên

## Cập nhật 04/11/2026: chạy parsing trước, TV5 validation sau

Theo phân công của nhóm, TV5 phụ trách báo cáo MS2 và validator MS2. TV1 tiếp tục
chuẩn bị snapshot, điều phối parsing, xuất bảng và xử lý lỗi tích hợp; không nhận
phần viết báo cáo MS2 của TV5.

Resolver, Hierarchy, Normalization, Bibliography và Deduplication đã có trên
`main` sau PR #9–#13. Validator MS2 chưa bàn giao. Không cần chờ validator để
chạy năm bước này và lấy bảng phục vụ kiểm tra; không tạo validator giả để đạt
`complete=true`. Các bước được lưu checkpoint để TV5 tiếp tục bằng `--resume`.

### Cách chạy trước khi có TV5

1. Sao chép đầu vào MS1 đã hoàn tất thành snapshot riêng. Giữ đường dẫn tương đối
   của discovery, checkpoint download/extract, `raw`, `corpus` và sidecar được
   checkpoint tham chiếu. Ghi nguồn và hash file; không lấy chỉ ZIP báo cáo/benchmark.
2. Chọn paper có đủ các version trong discovery. Paper chưa đủ dữ liệu vẫn phải
   ghi nhận thiếu/lỗi; `no_source` cần bằng chứng hợp lệ, không thay thế lỗi tải.
3. Dùng mã `main` mới nhất, cài dependencies từ `pyproject.toml`, chạy từ repo root:

```powershell
python main.py --stage parse --ms1-input artifacts/ms1-snapshot --output artifacts/ms2 --ids 2410.08024 --resume
```

Tên `artifacts/ms1-snapshot` là ví dụ, thay bằng snapshot thực tế. Luôn giữ MS1 và
MS2 ở hai root tách biệt. Với snapshot chỉ chứa source của một paper nhưng giữ
discovery manifest nhiều paper, phải dùng `--ids`; không chạy toàn snapshot đó.

4. Kiểm tra `manifests/stages.json`: năm bước đầu phải `success` hoặc `no_source`
   hợp lệ. Nếu chỉ `validate_parse=not_implemented`, ba bảng vẫn được giữ ở
   `tables/<paper_key>/`, cùng `references.bib`, manifest và occurrences theo version.
5. `parsing_summary.json` và `state/parsing/<paper_key>.json` vẫn `incomplete`,
   `complete=false`; CLI trả mã 1 khi còn thiếu validation. Mã 1 cũng có thể do lỗi
   đầu vào/parsing, nên phải đọc stage results, không mặc định mọi incomplete là
   do TV5. `parsing_manifest.complete=true` chỉ xác nhận export đủ version, không
   phải xác nhận validator đã chạy. Giữ nguyên các trạng thái và warnings này.

### Kết quả chạy thật đầu tiên để TV5 đối chiếu

Ngày 04/11/2026, TV1 đã sao chép paper **2410.08024 v1** từ thư mục dữ liệu TV5
trong [Drive chung](https://drive.google.com/drive/folders/123TUmjXMX38gKRLnJIKDl2yZF8jK6e9n).
Đã lấy manifest discovery gốc và đúng checkpoint, source archive, TeX/BibTeX,
download/extract metadata của paper được chọn; không sửa nội dung các file MS1.
Discovery gốc có sáu paper, nhưng snapshot mẫu này chỉ chứa source của paper trên.

- Cả năm bước parsing/export thực tế `success`; validator `not_implemented`.
- `elements.json`: 229 hàng; `hierarchy.json`: 231 hàng; `bibtex.json`: 117 hàng.
- Counts trước/sau dedup: elements 232 → 229; BibTeX 122 → 117.
- Có bốn cảnh báo `bib_field_conflict` về cách ghi tác giả và venue, cần TV5
  đối chiếu; không xem stage success là nghiệm thu nội dung.
- Chạy lại `--resume` tái sử dụng đủ năm checkpoint parsing/export; validator
  chưa có được thử lại. Hash của cả chín file MS1 tải xuống không đổi.

Artifact local (không đưa corpus vào Git):
`artifacts/ms1-tv5-2410.08024-20261104/` chứa snapshot và `snapshot-provenance.json`;
`artifacts/ms2-tv5-2410.08024-20261104/` chứa kết quả và checkpoint. Đây là một mẫu
thật một version, chưa đại diện cho đa version, `no_source` hay toàn dữ liệu nhóm.
TV5 cần có bản sao snapshot/output này trên máy chạy, không chỉ checkout tài liệu.

### TV5 tiếp tục từ đâu

Thực hiện adapter `validate_parse` theo [mục 9 của hợp đồng](MS2_CONTRACTS.md#9-bàn-giao-validator-ms2-cho-tv5).
Sau khi module thật được tích hợp, chạy lại cùng lệnh với cùng snapshot/output và
`--resume`. Các stage không đổi sẽ được tái sử dụng, validator được chạy mới;
không phải crawl lại hay xóa bảng để nối TV5. TV5 dùng kết quả validation và số
liệu có nguồn để viết báo cáo MS2; mẫu trên chưa thay cho nghiệm thu toàn corpus.

## Lịch sử chuẩn bị hợp đồng phần A

Ngày chuẩn bị: 29/10/2026. Đây là tài liệu để người dùng chuyển cho nhóm;
chưa gửi thông điệp ra ngoài và chưa có xác nhận TV2–TV5.

Cập nhật 31/10/2026: theo người dùng, A5 tiếp tục chờ nhóm hoàn tất dữ liệu MS1
để đối chiếu/bàn giao thực tế. Ma trận bên dưới ghi nhận các xác nhận kỹ thuật
đã có; không đánh đồng việc có schema/mock tests với nghiệm thu corpus.

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
| TV3 | Envelope `stages.hierarchy`, cleaned_content, đánh lại position sau tách câu | Đã tích hợp qua PR #13; chưa nghiệm thu toàn corpus |
| TV4 | Nhánh bibliography đọc resolve; gom versions; ba bảng, occurrences và manifest | Đã tích hợp trên main; chưa nghiệm thu toàn corpus |
| TV5 | Validation MS2 riêng, root không có cạnh null, empty coverage, counts trước/sau dedup | Chưa bàn giao; xem hướng dẫn tiếp tục ở trên |

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

- Resolver phần B đã triển khai trên `feature/m2-tv1-resolver`, kiểm thử bằng
  corpus/checkpoint giả lập; xem `MS2_RESOLVER.md`. Phần C đã có Orchestrator/CLI
  offline trên `feature/m2-tv1-orchestrator`, ghép Resolver và Hierarchy đã sửa.
  Normalization/Bibliography/Deduplication đã tích hợp trên main; validator MS2
  còn chờ bàn giao. CLI thật báo incomplete khi chưa validation. Adapter TV5
  dùng module/stage riêng.
- Đã chạy một paper thật như phần cập nhật ở trên; chưa nghiệm thu toàn MS1/MS2.
- Dùng fixture tạm để phát triển ngay; khi có mẫu MS1 hoàn tất, cần nhận cả
  discovery, checkpoint download/extract và sidecar bằng chứng liên quan.
- Mẫu snapshot hiện có được sao chép từ dữ liệu Drive do người dùng bàn giao;
  không đọc trực tiếp output đang chạy làm nguồn nghiệm thu.
- Nhánh bàn giao phần A: `feature/m2-tv1-contracts`, tách từ `origin/main`
  (`c90e79c`) theo yêu cầu người dùng commit/push; không ghép vào PR MS1.
- Tiến độ cá nhân TV1 được theo dõi ngoài repo, không tạo thêm checklist dùng chung.

Cập nhật kiểm thử phần C ngày 02/11/2026: 29 tests Orchestrator mới, gồm graph,
resume/force, thêm version, hash artifact, snapshot đổi khi export, input readonly
và Windows junction. Toàn repo **260 passed, 1 skipped, 61 subtests passed**;
test skip là trường hợp tạo symlink của Resolver trên host Windows. Hướng dẫn
chạy và hợp đồng được cập nhật ngay trong README/MS2_CONTRACTS, không thêm
file kế hoạch cá nhân cho phần C. Tích hợp validator thật của TV5 (C8) và nghiệm
thu trên corpus thật vẫn chưa hoàn tất.
