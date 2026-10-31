# Hợp đồng Milestone 2 — baseline 2.0

Ngày: 29/10/2026. Phạm vi: A của checklist TV1. Người dùng đã chốt bảng
`hierarchy` giữ đúng bốn cột theo đề. Các quyết định nội bộ khác là baseline
triển khai do TV1 đề xuất, chờ TV2–TV5 xác nhận trước tích hợp.

Nguồn ưu tiên: [đề MS2 chính thức](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/parsing/).
Ví dụ executable: `tests/parsing/ms2_contract_examples.py`; chạy
`python -m pytest tests/parsing/test_ms2_contracts.py -q` từ repo root.
File này đặc tả giao tiếp. Resolver đã triển khai trong phần B với tests giả lập;
xem [MS2_RESOLVER.md](MS2_RESOLVER.md). Orchestrator MS2 chưa triển khai.

## 1. Audit MS1 (A1)

- `DiscoveryRecord` chứa `versions`, metadata và trạng thái discovery. Chỉ nhận
  discovery `success` đủ danh sách v1..latest; danh sách version của record lỗi
  không chứng minh độ bao phủ đầy đủ.
- `VersionRecord` có version, versioned_id, source/corpus path và metadata;
  **không có status**. Paths dùng `paper_key()` hiện có: `hep-th/9901001`
  thành `hep-th_9901001`, ID hiện đại giữ dấu chấm trong tên thư mục.
- Checkpoint: `state/stages/download/<paper_key>/<N>.json` và
  `state/stages/extract/<paper_key>/<N>.json`; là StageResult serialize.
- Crawl orchestrator hiện ghi extract `skipped` khi download `no_source`.
  Vì vậy không đòi extract `success` cho version empty đã xác nhận.
- Extractor giữ `.tex`/`.bib` nhưng còn sidecar `extract_metadata.json` trong
  corpus; resolver chỉ đọc file source phù hợp, không coi sidecar là TeX.
- Extractor có thể trả `no_source` khi source thiếu/rỗng; có thể trả `success`
  nếu chỉ còn `.bib`. Hai trường hợp này cần kiểm tra bổ sung ở điều kiện bàn giao.
- Tại thời điểm audit phần A, module parsing là slot `NotImplementedError`. Validator
  `export/validation.py` đang phục vụ MS1, không thay thế bằng validator MS2.
- Khi audit, repo local ở `feature/tv5-ms1-benchmark-report`, base `f70c0b1`.
  Theo yêu cầu push sau đó, phần A được tách sang `feature/m2-tv1-contracts`
  từ `origin/main` (`c90e79c`), không đưa vào PR MS1.
  Audit này kiểm tra code/hợp đồng, chưa audit corpus đang crawl.

## 2. Schema và phạm vi phiên bản

`common/schemas.py` bổ sung `ElementRow`, `HierarchyRow`, `BibtexRow`.
Các dataclass mô tả hàng dữ liệu, không thay cho validator runtime của TV5.

| Bảng | Các trường theo thứ tự |
| --- | --- |
| elements | `element_id: str`, `arxiv_id: str`, `kind: str`, `content: str` |
| hierarchy | `arxiv_id: str`, `version: int`, `element_id: str`, `parent_id: str` |
| bibtex | `arxiv_id: str`, `citation_key: str`, `entry_type: str`, `title: str`, `authors: list[str]`, `year: int | None`, `doi: str | None`, `journal: str | None`, `url: str | None` |

ID/khóa bắt buộc là string không rỗng; version là int >=1 (không nhận bool).
`kind`: `document`, `heading`, `itemize`, `item`, `sentence`, `block_formula`,
`figure` (bao gồm table). `prose_block` chỉ ở cây thô, không có trong bảng cuối.
`entry_type` dùng tên loại BibTeX chữ thường; không giới hạn bốn loại ví dụ.
Thiếu title dùng `""`, authors dùng `[]`, year/doi/journal/url dùng null.
Không suy đoán metadata không biết. Root là element document riêng mỗi version;
**không tạo hierarchy row cho root**, nên parent_id trong bảng luôn khác null.
Version empty không tạo root hay parsed elements; có record trạng thái riêng.

Element ID bắt đầu bằng khóa publication và discriminant duy nhất trong paper.
Baseline dùng prefix `arxiv_id.replace('.', '-').replace('/', '_') + '#'`,
rootN/hN/sN/eqN/figN/listN/itemN. Đây là namespace element, khác `paper_key()`
của thư mục. Cấp ID theo version tăng dần và thứ tự preorder để ổn định.
Dedup theo `(kind, cleaned_content)` trong cùng paper; document root giữ riêng
theo version. Không dedup xuyên paper. Đồng bộ citation **trước** dedup nội dung.

`SCHEMA_VERSION=1.0` vẫn là default của DiscoveryRecord/StageResult MS1.
MS2 phải truyền tường minh `schema_version=PARSING_SCHEMA_VERSION` (2.0)
vào StageResult. Mọi JSON object artifact MS2 có schema_version 2.0;
ba bảng là JSON arrays đúng cột đề, phiên bản/schema đặt ở manifest phụ.

## 3. Input root và adapter envelope (A3)

Giữ chữ ký:

```python
def run(record: dict, output_dir: Path, *, version: dict | None = None,
        upstream: dict | None = None) -> StageResult:
    ...
```

MS2 nhận **input root MS1 độc lập** và **output root MS2**. Không ghi vào input.
Orchestrator truyền context cho mọi adapter qua `upstream`:

```python
{
    "context": {
        "input_root": "/runtime/path/to/frozen-ms1-sample",
        "input_snapshot_id": "snapshot-content-fingerprint"
    },
    "stages": {"resolve": resolve_result.to_dict()}
}
```

`input_root` là string đường dẫn runtime (có thể tuyệt đối), **không serialize
vào artifact**. Các path trong StageResult MS1 phân giải theo input root;
StageResult MS2 theo output root. Path source trong gathered/source_spans phân
giải theo input root. Mọi path serialize dùng `/`, tương đối, không `..`, không
thoát root sau resolve symlink. Consumers nhận cùng context, không đoán root.

| Adapter / stage | version | Nội dung upstream ngoài context |
| --- | --- | --- |
| resolver / `resolve` | VersionRecord dict | `stages.download`, `stages.extract`: checkpoint MS1 |
| hierarchy / `hierarchy` | VersionRecord dict | `stages.resolve` |
| normalization / `normalize` | VersionRecord dict | `stages.hierarchy` |
| bibliography / `bibliography` | VersionRecord dict | `stages.resolve` (đọc gathered trực tiếp) |
| deduplication / `deduplicate` | null | `versions`: list được mô tả bên dưới |
| validator MS2 / `validate_parse` | null | `stages.deduplicate` |

Đầu vào dedup là envelope có `context`, `stages: {}` và `versions` tăng dần:

```python
{
    "version": 1,
    "normalize": normalized_result.to_dict(),
    "bibliography": bibliography_result.to_dict()
}
```

Mỗi phần tử có đúng một version kỳ vọng; giữ cả version `no_source`, không
lọc mất khi gom paper. Record discovery giữ danh sách version kỳ vọng để đối chiếu.
Một version lỗi/thiếu/chưa sẵn sàng chặn kết quả cuối cấp paper; báo `skipped`
với ErrorInfo chỉ rõ upstream. Không đánh `success` cho paper còn thiếu version.
Paper tất cả version empty vẫn xuất elements/hierarchy rỗng và manifest coverage,
status `no_source`. Bibliography vẫn đọc inventory `.bib` khi resolve no_source;
nếu có entries thì giữ trong bibtex, chỉ ghi bibtex=[] khi không có references.
Bibliography trả success nếu đọc được entries, no_source với entries=[] khi không
có nguồn tham khảo. Normalize vẫn trả no_source với nodes=[] cho version đó.
Paper có version parse thành công và version empty có thể `success`.

## 4. Điều kiện ready/empty/error (A6)

Kiểm tra schema/checkpoint đúng stage, paper, version và artifact không vượt root.
Discovery phải thành công và đủ version trước khi nghiệm thu cấp paper.

| Bằng chứng MS1 | Quyết định MS2 |
| --- | --- |
| Download/extract success, corpus đã ghi xong, tồn tại TeX | Ready cho resolve |
| Download no_source, sidecar xác nhận PDF-only, extract skipped/no_source | Empty hợp lệ, giữ record no_source |
| Download success với source tồn tại không rỗng, extract no_source xác nhận không có TeX/BibTeX | Empty hợp lệ nếu bằng chứng nhất quán |
| Extract success nhưng corpus chỉ có .bib, inventory xác nhận không có TeX | Empty về body; giữ file BibTeX trong inventory/provenance để không mất thông tin |
| Không có checkpoint hoặc producer còn chạy | Chưa sẵn sàng, orchestration skipped; không suy ra no_source |
| Source/corpus thiếu dù checkpoint success, source rỗng, checkpoint mâu thuẫn | data_error, yêu cầu kiểm tra/bàn giao lại |
| Download lỗi HTTP/API, extract lỗi giải nén | Upstream lỗi; skipped ở MS2, giữ nguyên nguyên nhân |
| Download no_source chỉ dựa vào HTML hoặc lý do không xác minh được | Chưa đủ bằng chứng empty; data_error cần review |

Không đọc corpus đang ghi. Giai đoạn phát triển dùng bản sao đã hoàn tất cùng
checkpoint/sidecar liên quan, output riêng. Snapshot ID phải nhận diện nội dung
discovery/version list, checkpoint và source inventory (path + checksum), không
dùng thời điểm chạy hoặc tên thư mục làm bằng chứng bất biến. C1–C7 sẽ triển khai
kiểm tra fingerprint và invalidation; phần A chỉ chốt hợp đồng.

## 5. Artifact version-level (A4/A7)

Mọi object dưới đây có header `{schema_version, arxiv_id, version}`;
schema_version là `"2.0"`, warnings là list `{code, message, source_path}`
(source_path có thể null). Không dùng warning thay cho lỗi làm mất nội dung.

### gathered.json — `parsed/<paper_key>/vN/gathered.json`

Trường bắt buộc ngoài header:

- `is_empty_source: bool`, `main_tex_path: str | null`.
- `included_files: list[str]`: file include duy nhất theo thứ tự gặp đầu tiên,
  không gồm root; lặp include vẫn phải giữ trong merged text/source_spans.
- `ignored_files: list[str]`: TeX không đi tới được từ root, không xóa source.
- `merged_body_tex: str`, `preamble_tex: str` (không chứa bibliography trong body).
- `raw_bibitems: list[str]`: giữ lệnh bibitem và citation key nguyên bản.
- `bib_file_paths: list[str]`: inventory mọi `.bib`; `referenced_bib_file_paths`
  là tập con đã tham chiếu qua lệnh bibliography được hỗ trợ.
- `input_snapshot_id: str`, `warnings: list`, `source_spans: list`.

source_spans mỗi mục có `start`, `end` (offset code point Python, nửa mở trong
merged_body_tex), `source_path` (input-relative), `source_start`, `source_end`
(offset trong source gốc). Span dùng để truy vết, có thể bỏ trống với cảnh báo
khi không ánh xạ được; không bịa offset sau biến đổi.

Empty hợp lệ có body/preamble rỗng, root null, included/ignored/raw_bibitems
rỗng; inventory BibTeX vẫn giữ nếu có, warnings ghi lý do. Ví dụ PDF-only có
cả hai danh sách bib rỗng. Không có no_source chỉ vì không tìm thấy root.

### raw_tree.json — `parsed/<paper_key>/vN/raw_tree.json`

`nodes: list`, `warnings: list`. Mỗi node bắt buộc:
`temp_id: str`, `parent_temp_id: str | null`, `position: int >=0`,
`kind: str`, `raw_content: str`. temp_id duy nhất trong version.
Root document parent null, position 0. position là thứ tự con, liên tục từ 0
trong từng parent; nodes xếp preorder. Heading có thể bổ sung `heading_rank`
và `level`, nhưng không dùng heading_rank thay depth. Prose còn là prose_block.

### normalized_tree.json — `parsed/<paper_key>/vN/normalized_tree.json`

Cùng cấu trúc cây, thay `raw_content` bằng `cleaned_content: str`.
Mỗi prose_block được thay bằng sentence leaves; cấp temp_id mới không trùng
và tính lại position. Giữ cấu trúc, citation keys và provenance nếu có.
Không có prose_block ở output này. Empty tree có nodes=[] (không root giả).

### raw_bibtex.json — `parsed/<paper_key>/vN/raw_bibtex.json`

`entries: list[BibtexRow dict]`, `warnings: list`. Chưa dedup xuyên version;
giữ citation_key gốc, dữ liệu thô bổ sung có thể đặt ở sidecar provenance.
Entries không có key hoặc không parse được phải ghi cảnh báo/lỗi tường minh.
Empty bibliography hợp lệ là entries=[]; no_source không đồng nghĩa lỗi parser.

## 6. Kết quả paper-level và occurrences

`tables/<paper_key>/elements.json`, `hierarchy.json`, `bibtex.json` là ba arrays.
Mỗi object trong array có đúng các trường ở mục 2, không nhét metadata vào hàng.
Không có parent null ở hierarchy; tạo một hàng cho mỗi cạnh occurrence, **không
loại hàng trùng** khi hai lần xuất hiện có cùng element và parent sau dedup.
Thứ tự JSON array không được dùng như thay thế cho occurrence sidecar.

`parsed/<paper_key>/vN/occurrences.json` có header và `occurrences: list`:

```json
{
  "schema_version": "2.0", "arxiv_id": "2310.12345", "version": 1,
  "occurrences": [
    {"occurrence_id": "o1", "parent_occurrence_id": null, "position": 0, "element_id": "2310-12345#root1"},
    {"occurrence_id": "o2", "parent_occurrence_id": "o1", "position": 0, "element_id": "2310-12345#h1"},
    {"occurrence_id": "o3", "parent_occurrence_id": "o2", "position": 0, "element_id": "2310-12345#s1"},
    {"occurrence_id": "o4", "parent_occurrence_id": "o2", "position": 1, "element_id": "2310-12345#s1"}
  ]
}
```

Occurrence IDs duy nhất trong version, parent thuộc cùng version, không chu trình.
Giữ mọi lần xuất hiện, kể cả heading/component có chung element_id; parent luôn
tham chiếu occurrence cụ thể. Sidecar là đầu ra bổ sung của TV4 sau dedup và
rename citations; không phải cột bổ sung của hierarchy. Cây empty có occurrences=[].

`tables/<paper_key>/parsing_manifest.json` bắt buộc có schema_version, arxiv_id,
input_snapshot_id, expected_versions (list[int]), complete (bool), versions
(list `{version, status, reason, artifacts}`), tables (map tên bảng → relative path)
và counts (object: elements_before/after, bibtex_before/after là int >=0).
status mỗi version ghi rõ success/no_source/skipped/data_error/error; reason null
hoặc string; artifacts có occurrences path nếu đã xuất. Counts trước dedup lấy
từ các artifact version, không suy ngược từ bảng cuối. Manifest coverage giữ empty.

StageResult deduplicate khai báo artifacts `elements`, `hierarchy`, `bibtex`,
`parsing_manifest`. Consumer lấy occurrences từ manifest. Validator riêng của TV5
dùng stage `validate_parse`, artifact `validation` tại
`validation/parsing/<paper_key>.json`; module đề xuất
`reporting/milestone2/validation.py` như task TV5, chưa triển khai trong A.

## 7. Status, checkpoint và bàn giao

MS2 chỉ dùng statuses đã có: success/no_source/data_error/error/skipped và
not_implemented cho slot chưa bàn giao. ErrorInfo bắt buộc với lỗi/bị chặn;
version không có source hợp lệ có reason trong manifest, không tính là lỗi.
Không thêm status mới làm thay đổi validator trạng thái MS1.

Stage success/no_source phải ghi artifact JSON có cấu trúc trước khi trả về;
không ghi file nửa chừng (atomic write). MS2 checkpoint dự kiến
`<ms2-output>/state/stages/<stage>/<paper_key>/<N|paper>.json`.
Artifact version-level không lưu absolute input_root. Mỗi checkpoint resume MS2
có sidecar dependency fingerprint, gồm snapshot/source hashes, phiên bản module
và schema; thiếu/mismatch phải chạy lại. Thêm version hoặc thay upstream làm mất
hiệu lực dedup/validation paper. C triển khai chi tiết, không sửa resume MS1.

Các JSON ví dụ trong Python là mẫu hợp đồng; không phải số liệu thật hay module
đã nghiệm thu. TV2–TV5 cần xác nhận phần phụ trách trong `MS2_HANDOFF.md`.
