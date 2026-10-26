# Hợp đồng input/output v1

Đây là baseline triển khai của TV1 để các thành viên review và tích hợp. Chưa có xác nhận thực tế của TV2–TV5. Thay đổi phá vỡ hợp đồng cần tăng `schema_version` và cập nhật consumer, tests, tài liệu trong cùng PR. Cấu trúc trong yêu cầu của người dùng là cấu trúc chính; các tên `src/milestone1` trong kế hoạch được ánh xạ sang `src/arxiv_pipeline/crawling`.

## Discovery → TV2 và TV4

`discover(input_id, client) -> DiscoveryRecord`; `discover_many(ids, client)` trả iterator. JSON là `record.to_dict()`. `manifests/discovery.json` là mảng record của lần chạy hiện tại.

| Trường | Kiểu và ý nghĩa |
| --- | --- |
| schema_version | Chuỗi `1.0` |
| input_id | Chuỗi gốc, đã bỏ khoảng trắng ngoài |
| arxiv_id | ID gốc không có version, hoặc null nếu sai cú pháp |
| requested_version | Số nguyên hoặc null; đầu vào version hợp lệ vẫn khám phá toàn bộ version |
| status | `success`, `invalid_id`, `not_found`, `api_error`, `data_error` |
| timestamp | ISO 8601 có UTC offset |
| attempts | Tổng HTTP attempts của lần discovery này, gồm retry, không phải số retry |
| error | null hoặc `{kind, message, retryable, http_status}` |
| metadata | null hoặc object mô tả bên dưới |
| versions | Mảng tăng dần từ v1 đến latest; có thể chưa đủ nếu lỗi giữa chừng |

Chỉ chuyển record `status=success` xuống các module sau. `versions` của record lỗi chỉ phục vụ chẩn đoán, không được xem là đầy đủ. ID trùng chính xác sau strip được chạy một lần; các biểu diễn khác nhau của cùng ID được giữ riêng để truy vết input. Các bước sau dùng cùng khóa paper/version.

`metadata` gồm `arxiv_id`, `title`, `authors: list[str]`, `submission_date`, `revised_dates: list[str]`, `venue: str|null`, `latest_version: int`, `abstract`, `categories`, `doi: str|null`, `source=arxiv_api`, `retrieved_at`. `submission_date` lấy từ v1; `revised_dates` lấy `updated` của v2..vn. Không suy đoán ngày từ ID, không tự tạo venue. Một lần query latest và một query cho mỗi v1..v(n−1).

Một phần tử `versions`:

```json
{
  "version": 1,
  "versioned_id": "2301.00001v1",
  "source_url": "https://arxiv.org/src/2301.00001v1",
  "source_path": "raw/2301.00001/v1/source",
  "corpus_path": "corpus/2301.00001/v1",
  "submission_date": "2023-01-01T00:00:00Z",
  "updated_date": "2023-01-01T00:00:00Z",
  "title": "Example title of this version",
  "authors": ["Alice Example"],
  "venue": null
}
```

`title`, `authors`, `venue` (arXiv `journal_ref`, null nếu không có) là metadata của chính version đó, vì chúng có thể đổi giữa các version; các trường cấp paper trong `metadata` lấy từ version mới nhất. Ba trường này được thêm sau, có giá trị mặc định (`null`, `[]`) nên `schema_version` vẫn là `1.0`; checkpoint Discovery cũ thiếu chúng và được stage `metadata` ghi là trường thiếu.

Ví dụ trên là dữ liệu minh họa schema, không phải metadata đã truy vấn. Legacy ID `hep-th/9901001` dùng khóa thư mục `hep-th_9901001`. Mọi đường dẫn trong JSON là đường dẫn tương đối với `--output`, dùng `/`. Không nhúng đường dẫn tuyệt đối hoặc `..`.

## Giao diện adapter cho TV2–TV5

```python
def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    ...
```

Import kiểu trả về từ `arxiv_pipeline.common.schemas`. Adapter chịu trách nhiệm kiểm tra nội dung, ghi artifact an toàn/atomic và phân loại lỗi. Không trả `success` nếu chưa ghi kết quả. Orchestrator kiểm tra stage/paper/version và mọi artifact tồn tại bên trong output root. Mỗi adapter phải trả ít nhất một artifact cho `success`; với kết quả rỗng hợp lệ có thể ghi JSON rỗng kèm status phù hợp.

| Chủ sở hữu | Module / stage | Đầu vào bổ sung | Artifact quy ước |
| --- | --- | --- | --- |
| TV2 | crawling/downloader.py / `download` | version | `source` trỏ đúng `version.source_path`; lưu bytes nguyên gốc, nhận diện định dạng từ content |
| TV3 | crawling/extractor.py / `extract` | version, upstream là download StageResult đã serialize | `corpus` trỏ `version.corpus_path`, chỉ giữ .tex/.bib |
| TV4 | crawling/metadata.py / `metadata` | cả paper | `metadata`: `metadata/{paper_key}.json` |
| TV4 | crawling/references.py / `references` | cả paper | `references`: `references/{paper_key}.json` |
| TV5 | export/validation.py / `validate` | cả paper; đọc các checkpoint stage đã ghi trên đĩa | `validation`: `validation/{paper_key}.json` |

TV3 lấy đường dẫn tải thực tế từ `upstream['artifacts']['source']`; không đoán phần mở rộng. TV2 đặt Content-Type, checksum, định dạng trong sidecar JSON và khai báo thêm artifact `download_metadata`. Corpus luôn tách theo version; không ghi đè v1 bằng v2. `--force` cho phép làm mới cùng version, adapter phải thay thế atomic và tránh tạo corpus gồm file cũ lẫn mới. `no_source`/PDF-only phải giữ record rõ ràng, không coi lỗi HTTP tùy ý là `no_source`. Extract nhận `skipped` khi download không thành công; `download=no_source` là kết quả terminal được báo cáo riêng.

`StageResult` gồm `schema_version`, `stage`, `paper_id`, `version` (null cho stage cấp paper), `status`, `timestamp`, `artifacts: dict[str,str]`, `error`, `attempts`, `size_bytes`. Status cho adapter: `success`, `no_source`, `not_indexed`, `no_data`, `api_error`, `data_error`, `error`. Orchestrator bổ sung `not_implemented` và `skipped`. Dùng `ErrorInfo` cho lỗi; `attempts` tính tổng attempts trong lần thực thi adapter, `size_bytes` là dung lượng output do adapter tạo, byte không âm.

TV4 ghi references dạng mảng object với `arxiv_id` (paper đang xét), `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id`, `missing_fields: list[str]`. Trường scalar thiếu là null, danh sách thiếu là []; giữ reference không có arXiv ID. Sidecar provenance chứa nguồn, retrieved_at và trạng thái không index/thiếu dữ liệu/lỗi API. Không đưa Semantic Scholar ID thành arXiv ID.

## TV5 đọc trạng thái và khả năng chạy lại

```text
<output>/
  manifests/discovery.json
  manifests/stages.json
  summary.json
  state/discovery/<sha256(input_id)>.json
  state/stages/<stage>/<paper_key>/<version_number|paper>.json
  logs/pipeline.log
  raw/<paper_key>/vN/source
  corpus/<paper_key>/vN/...
  metadata/<paper_key>.json
  references/<paper_key>.json
  validation/<paper_key>.json
```

Checkpoint được ghi ngay sau mỗi input/stage; manifest và summary là snapshot lần chạy hoàn tất gần nhất. Sau gián đoạn, TV5 đọc checkpoint; chạy lại `--resume` để tạo lại manifest. Không chạy đồng thời nhiều CLI trên cùng output root. Một client gửi request tuần tự, tối thiểu 3 giây; nhiều process/máy phải tự phối hợp giới hạn chung. Chạy không có `--resume` hoặc có `--force` sẽ làm mới các input được chọn, không xóa dữ liệu paper khác.

Resume chỉ tái sử dụng discovery `success` có schema/version list hợp lệ. Input lỗi đều được thử lại. Stage terminal được reuse nếu artifact vẫn tồn tại; success thiếu artifact phải chạy lại. Không kiểm checksum/nội dung artifact tại orchestrator: TV2/TV3 phải kiểm tra tính toàn vẹn khi thực thi, TV5 kiểm toàn bộ corpus. Khi thay code hoặc cần kiểm lại dữ liệu/thông tin latest mới, dùng `--force` vì resume dùng snapshot cũ. Không tự skip lỗi `api_error`/`data_error`/`not_implemented`.

Exit code: `0` khi các bước được yêu cầu hoàn tất (các outcome thiếu source/index/data được đếm riêng); `1` khi có input lỗi, stage lỗi/chưa bàn giao hoặc lỗi I/O; `2` khi cấu hình CLI sai; `130` khi người dùng ngắt. `summary.complete` biểu thị các bước yêu cầu đã kết thúc hợp lệ, không phải mọi paper có source. Tỷ lệ thành công phải dựa vào từng status, không chỉ exit code. Khi downstream chưa bàn giao, full crawl báo incomplete và exit 1. TV1 chưa triển khai benchmark hoặc export Parquet của TV5.

## Phân biệt lỗi Discovery

- `invalid_id`: cú pháp, tháng hoặc độ dài ID sai; không gọi mạng.
- `not_found`: API trả Atom feed hợp lệ không có entry, hoặc requested version lớn hơn latest.
- `api_error`: HTTP/network/timeout hoặc Atom entry thông báo lỗi API; có retry với network, 408, 429 và 5xx; Retry-After được tôn trọng.
- `data_error`: XML sai, sai namespace, thiếu metadata, ID/version phản hồi không khớp, thiếu version trung gian.

Tài liệu API: [arXiv API manual](https://github.com/arXiv/arxiv-docs/blob/develop/source/help/api/user-manual.md). Danh sách version v1..vn dựa trên latest vn của API, và xác minh metadata mỗi version. Đây là snapshot tại thời điểm discovery.
