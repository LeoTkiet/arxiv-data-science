# T3-04: chuẩn bị XLSX, thu nhãn và audit

Owner: TV3. Nhánh triển khai: `feature/m3-tv3-manual-labeling` theo quy ước `feature/...` của nhóm. Reviewer bàn giao: TV5; TV1 review giao diện đầu vào.

Tài liệu này mô tả code để tái chạy. Hướng dẫn gán nhãn đã chốt trên Drive được giữ nguyên. Corpus, XLSX thật, nhãn, kết quả và ZIP nằm ngoài Git trong `artifacts/` hoặc Drive.

## Code và phạm vi

- `matching/annotation_workbook.py`: xuất phiếu chỉ có `Gan_nhan` và `Candidates`, ID lưu dưới dạng text, đọc lại XLSX từ Excel/Google Sheets.
- `matching/manual_workflow.py`: kiểm tra dữ liệu staged, xuất phiếu theo phân công, thu kết quả và audit nhãn accepted.
- `scripts/manual_labeling.py`: CLI chạy từ checkout, không cần cài package hoặc thư viện Excel bên ngoài.
- `configs/ms3_manual_assignments.json`: 25 IDs đã chọn, mỗi người 5 bài; không chứa nội dung corpus hay nhãn.

Chạy trên Python 3.10+. Tests cần pytest. Toàn bộ workflow offline, không gọi Drive, arXiv, Semantic Scholar hay model.

## 1. Chuẩn bị đầu vào

Nhận các artifacts của cùng snapshot từ bước staging/loader TV1. Với từng bài trong config:

```text
<inputs>/tvN/<paper_key>/
  bibtex.json
  references.json
  ms1_validation.json
  ms2_validation.json
  input_manifest.json
```

`paper_key` dùng quy tắc hiện có của repo (ID legacy đổi `/` thành `_`). `bibtex` là bảng M2 sau dedup, references là M1 gốc, gồm cả rows không có arXiv ID. `input_manifest.json` là parsing manifest M2, không phải một file tự khai readiness.

Code kiểm identity/schema, errors rỗng, validation valid, M2 complete, snapshot ID nhất quán, coverage mọi version 1..N, số rows so với counts và ít nhất 20 query keys duy nhất mỗi bài. MS1 cần outcome references success/no_data có bằng chứng; không biến not_indexed/API lỗi thành tập rỗng hợp lệ. References không có arXiv ID được loại khỏi candidate registry bởi contract.

Đây là kiểm tra artifacts staged và validation reports. Không thay thế loader TV1 kiểm toàn bộ producer checkpoints/dependencies/hashes của snapshot M1/M2. `production_snapshot_verified=false` trong output thể hiện giới hạn này; không dùng riêng lệnh prepare làm bằng chứng nghiệm thu pipeline/corpus.

Đóng băng input khi chạy. Không sửa report để ép valid/complete. File thiếu/hỏng/identity không khớp làm prepare thất bại.

```powershell
python scripts/manual_labeling.py prepare --assignments configs/ms3_manual_assignments.json --inputs artifacts/t3-04-selection/selected_inputs --output artifacts/t3-04-forms
```

Output:

```text
<output>/
  annotation_tv1.xlsx ... annotation_tv5.xlsx
  publications/<paper_key>/bundle.json
  prepared_manifest.json
```

Thư mục output phải chưa tồn tại. Code không ghi đè phiếu đã làm. Manifest công bố cuối cùng, chứa assignments, source hashes và bundle hashes; thư mục bị gián đoạn chưa có manifest không được dùng collect. Nếu lần chạy bị gián đoạn, giữ lại bằng chứng và chạy sang output mới.

## 2. Gán nhãn và review

Tạo bản sao XLSX để điền theo hướng dẫn riêng. Giữ nguyên 8 cột nguồn và các query rows. Reader chấp nhận thứ tự dòng thay đổi nhưng kiểm lại bằng `(arxiv_id, citation_key)`, không nối nhãn theo vị trí.

Reader hỗ trợ inline/shared strings, ô trống và số năm ở định dạng chuẩn. IDs/citation keys phải là text; số do Excel đổi kiểu bị báo lỗi để khôi phục từ phiếu gốc. Công thức, duplicate rows (kể cả chưa gán), query bị xóa, sửa metadata nguồn, nhãn sai người hoặc groundtruth ngoài candidate pool đều bị chặn.

Code giữ nguyên pending/accepted/rejected và không tự chuyển quyết định sang accepted. Nhãn uncertain phải có notes. Accepted chỉ được tính quota khi có reviewer thuộc nhóm, khác annotator, quyết định matched/unmatched và khóa hợp lệ.

## 3. Thu kết quả

Mỗi `--workbook` khai báo rõ người phụ trách. Có thể nộp từng thành viên để xem tiến độ; thiếu các thành viên còn lại thì audit chưa đạt quota nhóm.

```powershell
python scripts/manual_labeling.py collect --prepared artifacts/t3-04-forms --workbook tv1=artifacts/returned/annotation_tv1_reviewed.xlsx --workbook tv2=artifacts/returned/annotation_tv2_reviewed.xlsx --workbook tv3=artifacts/returned/annotation_tv3_reviewed.xlsx --workbook tv4=artifacts/returned/annotation_tv4_reviewed.xlsx --workbook tv5=artifacts/returned/annotation_tv5_reviewed.xlsx --output artifacts/t3-04-labels
```

Nếu nhập bản đề xuất AI chưa được người review xác nhận:

```powershell
python scripts/manual_labeling.py collect --prepared artifacts/t3-04-forms --workbook tv3=artifacts/t3-04-selection/annotation_tv3_annotated_draft.xlsx --origin ai_assisted --output artifacts/t3-04-tv3-draft-audit
```

`--origin ai_assisted` được lưu trong provenance và không chấp nhận workbook tự ghi accepted. Chỉ nhập bản người thực sự đã kiểm tra/xác nhận bằng origin human; tên annotator trong phiếu không chứng minh human review. LabelRecord dùng schema manual hiện có nhưng nhãn pending không phải supervised gold và không được tính quota.

Các output:

- `labels.json`: LabelRecord schema 3.0, gồm cả pending/uncertain để QA; chưa phải toàn bộ là nhãn nghiệm thu.
- `manual_audit.json`: lỗi và tiến độ theo bài/người; `ready_for_handoff` là cờ nghiệm thu nhãn.
- `label_manifest.json`: hashes workbook, labels, prepared manifest và origin; không ghi đường dẫn máy cá nhân.

Không dùng riêng số dòng hoặc nhãn `label_source=manual` để suy rằng quota đã đạt.

## 4. Chạy audit lại

```powershell
python scripts/manual_labeling.py audit --prepared artifacts/t3-04-forms --labels artifacts/t3-04-labels/labels.json --output artifacts/t3-04-recheck
```

Audit đếm distinct query keys có quyết định hợp lệ và review accepted: ít nhất 25 publications, mỗi bài ít nhất 20 entries; đủ roster TV1–TV5, mỗi người ít nhất 5 bài đạt ngưỡng và ít nhất 100 entries; tổng ít nhất 500 entries. Trường hợp một người có `19+21+20+20+20=100` entries vẫn không đạt 5 bài. Duplicate labels không làm tăng quota, uncertain/pending/rejected không được tính; lỗi FK/owner/reviewer chặn bàn giao.

Exit codes: `0` prepare thành công hoặc collect/audit ready; `1` collect/audit có kết quả nhưng dữ liệu/quota chưa đạt; `2` đầu vào/lệnh lỗi. Output đã tồn tại bị từ chối, không có `--force` ghi đè nhãn.

## Kiểm thử và bằng chứng

```powershell
python -m pytest tests/matching -q
```

Tests offline gồm ID text/numeric, shared strings, công thức, bảo toàn pending, source metadata bị sửa, duplicate/query bị xóa, invalid targets/owner, snapshots/hash, AI draft, quota 19/21, thiếu thành viên, self-review và vòng prepare → collect → audit trên 500 query synthetic. Synthetic chỉ kiểm code, không chứng minh quota nhóm thật.

Không commit output smoke thật, file Drive, nhãn AI hay workbook vào repo. Việc merge PR và đánh dấu T3-04 Xong còn cần review và đủ nhãn thật; nhánh code này không tự xác nhận các điều kiện đó.
