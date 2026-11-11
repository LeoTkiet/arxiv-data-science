# Bàn giao tích hợp MS2 — TV1 và các thành viên

## Cách chạy phân tán theo thành viên

Theo phân công mới, mỗi thành viên chạy [MS2_Integration_CPU.ipynb](../colab/MS2_Integration_CPU.ipynb)
trên đúng data MS1 của mình. Chọn `MEMBER`, sửa `MS1_INPUT`, chạy SMOKE trước
rồi FULL. Range đọc tự động từ config nhóm; FULL chọn đủ 5.000 ID để không bỏ
sót discovery còn thiếu hoặc vô tình xử lý dải của người khác. Cùng dùng mã
production cố định `b932d98eae90d50611004ccc5e2d91709a77b7bf`.

| Thành viên | Dải lookup ID |
| --- | --- |
| TV1 | 2508.12872:2508.17871 |
| TV2 | 2506.15788:2506.20787 |
| TV3 | 2411.08799:2411.13798 |
| TV4 | 2410.18025:2410.23024 |
| TV5 | 2410.03025:2410.08024 |

Output tách theo `RUN_TAG`, chế độ và thành viên; không có writer ghi manifest
nhóm dùng chung. Sau FULL, bật `CREATE_ZIP` ở cell cuối để gửi ZIP cá nhân cho
TV1/TV5. ZIP phải chứa đủ output, không chỉ log hoặc ba bảng. TV1 đối chiếu năm
receipt, cùng code SHA, đúng range và các bài/version còn lỗi; TV5 xử lý thống
kê và báo cáo. Không chép đè các manifest của từng người lên nhau. Các mục hướng
dẫn chạy tập trung ở lịch sử bên dưới được thay bằng cách chọn thành viên này.

Notebook phân tán đã qua 23 kiểm thử liên quan, gồm đủ năm dải phân công,
thiếu discovery trong FULL, output/ZIP riêng từng người. Đã chạy SMOKE bằng
cell mới trên bốn bài thật/sáu version của TV1/TV5, validation và resume qua,
hash input không đổi. Chưa thực thi notebook trên dịch vụ Colab.

## Cập nhật 10/11/2026: nghiệm thu phần E

Sáu module thật đã có trên `main` tại `b123a8f`, gồm sửa validator và benchmark
của PR #19. TV1 tiếp tục phụ trách tích hợp; TV5 phụ trách QA, thống kê và viết
báo cáo MS2. Không cần mock adapter để chạy pipeline hiện tại.

Mẫu đầu vào được sao chép từ Drive, giữ nguyên byte và lưu provenance/SHA-256:

| Nguồn | Paper | Version | Trường hợp |
| --- | --- | --- | --- |
| TV1 | 2508.17481 | v1, v2 | Nhiều file TeX, include, BibTeX và dedup xuyên version |
| TV1 | 2508.17871 | v1, v2 | Mỗi version một file TeX và một `.bib`; có reference chỉ chứa ghi chú |
| TV1 | 2508.17740 | v1 | PDF-only, download/extract xác nhận `no_source` |
| TV5 | 2410.08024 | v1 | Hai file TeX và BibTeX, mẫu thật đã bàn giao trước |

Lần thử phát hiện `render_bibtex` xuất entry không có field khi schema chuẩn hóa
không giữ trường `note` của source. Tám entry như vậy ở 2508.17871 khiến TV5
không đọc lại được. Đã sửa exporter TV4 để ghi `title = {}` cho entry không có
field, giữ nguyên key và các giá trị rỗng; không điền dữ liệu suy đoán. Có tests
đọc lại BibTeX và chạy đủ pipeline với source reference chỉ chứa ghi chú.

Sau sửa, mẫu TV1 ba bài/năm version đã qua validation: 20 stage `success`, sáu
stage `no_source`, không có validation error. Có 214 cảnh báo `bib_field_conflict`
ở hai bài có source; đây là nội dung TV4/TV5 cần đối chiếu trước khi xác nhận
chất lượng thư mục trích dẫn. Mẫu TV5 được chạy lại cùng bộ mã trong đợt nghiệm
thu này; đọc receipt local bên dưới cho kết quả cuối và kiểm tra resume.

Lượt nghiệm thu mới trên cả bốn bài/sáu version tại commit `ef2845c` dùng đủ
module thật: 26 stage `success`, sáu stage `no_source`, cả bốn validator có
`valid=true`, không có error. Tổng đầu ra: 1.176 elements, 1.738 hierarchy,
371 bibtex; mọi hàng hierarchy đúng bốn cột theo đề. Có 218 cảnh báo
`bib_field_conflict` cần đối chiếu nội dung, gồm 214 từ mẫu TV1 và bốn từ mẫu TV5.
Resume tái sử dụng đủ 32 checkpoint; SHA-256 của 58 file MS1 không đổi.

Thử thêm trên bản sao output của 2508.17871: chọn riêng v1 làm kết quả incomplete
và vô hiệu hóa export cũ; chọn lại đủ v1/v2 giữ tám checkpoint version, dựng lại
deduplication/validation. Sửa thử một hàng elements cũng được phát hiện và tự
dựng lại đúng hash bảng ban đầu. Không sửa input hoặc output nghiệm thu chính
để tạo các tình huống này. Bằng chứng ở `local-receipt.json` và
`recovery-receipt.json` dưới output đợt nghiệm thu.

Toàn bộ tests: **432 passed, 1 skipped, 65 subtests passed**. Test skip cần quyền
tạo symlink trên Windows. Notebook được kiểm tra schema/cú pháp và chạy cell
nghiệm thu bằng fixture lẫn snapshot thật trên máy local; chưa mount/chạy Colab.

Các đường dẫn local dưới đây tính từ repo gốc, không phải worktree và không
được đưa corpus vào Git:

- `artifacts/ms1-integration-sample-20261110/`: 48 file từ Drive và manifest
  discovery gốc 5.000 bài, chỉ có source/checkpoint của ba bài TV1 đã chọn.
- `artifacts/ms1-tv5-2410.08024-20261104/`: chín file mẫu TV5, manifest sáu bài.
- `artifacts/ms2-e-acceptance-20261110/`: output theo thành viên, receipt nghiệm
  thu, đo tài nguyên, validation, checkpoint, bảng và bằng chứng resume.

Hai snapshot mẫu đều phải chạy bằng danh sách ID; không chạy mọi discovery
record khi chỉ tải một phần source. Receipt và corpus local không có trên Git;
người chạy lại cần bản sao dữ liệu hoặc truy cập Drive đã được nhóm bàn giao.

### Chạy tiếp toàn corpus

Dùng [MS2_Integration_CPU.ipynb](../colab/MS2_Integration_CPU.ipynb), chọn CPU,
chốt SHA code và snapshot đã dừng ghi. Mỗi `JOBS` chứa một thành viên, input,
output riêng, range và mẫu số phân công. Chạy mẫu trước; khi đủ snapshot thì đặt
`ids=None`. Notebook kiểm tra ID thực tế so với range, ghi missing/out-of-range,
tách kết quả source/empty/incomplete và giữ diagnostics theo stage.

Giữ `summaries/parsing_summary.json` cho số đo lượt chạy, `summaries/parse-run.json`
cho trạng thái trước resume, `summaries/resume-events.json` và `acceptance.json`
cho kết quả nghiệm thu. Chạy resume không sửa dữ liệu MS1. Không cộng lại toàn bộ
thư mục tables cũ để tính số liệu cho lượt chọn mới.

TV1 xử lý Resolver/Orchestrator; TV2 xử lý cấu trúc cây; TV3 xử lý chuẩn hóa;
TV4 xử lý reference/dedup/citation; TV5 xử lý validator, thống kê và báo cáo.
Các cảnh báo BibTeX cần quyết định nội dung, không xóa cảnh báo để đạt nghiệm thu.

E1 và phần chạy mẫu E2/E3 có bằng chứng local. **E4 và nghiệm thu toàn corpus
của E5 chưa hoàn tất**: chưa chạy Colab CPU-only và chưa có receipt đủ năm
thành viên. Audit MS1 TV1 bàn giao ngày 08/11 có 4.315/5.000 bài qua validator,
685 bài chưa qua; phải kiểm tra download/extract của từng version để xác định
lỗi nào chặn MS2. Lỗi metadata/references MS1 không tự động là lỗi source.

## Lịch sử 04/11/2026: chạy parsing trước, TV5 validation sau

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
