# Hợp đồng dữ liệu và giao tiếp Milestone 3

Ngày: 11/11/2026. Phiên bản đã chốt: `3.0`.
Trạng thái: nhóm đã chốt contract theo xác nhận của người dùng. Đã triển khai
schema, validators và compatibility fixtures; API nghiệp vụ/pipeline ở mục 6
là giao diện bàn giao cho các task tiếp theo, chưa phải pipeline M3 đã chạy được.
TV1 sở hữu contract; TV5 review tích hợp. Thay đổi phá tương thích phải cập nhật
phiên bản và thông báo cho tất cả consumer trước khi merge.

Nguồn phân công: [TV1 trên Notion](https://app.notion.com/p/4159cc1ed390819399f3ef11bac38e7c).
Nguồn nghiệm thu: [đề MS3 chính thức](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/matching/).
Các lựa chọn API, artifact và xử lý lỗi dưới đây là contract nhóm đã thống nhất.

## 1. Điểm cần sửa trong kế hoạch

Đối chiếu đề chính thức ngày 11/11/2026, với nhóm `k=5`:

| Tiêu chí | Mức cần đạt |
| --- | --- |
| Manual pool | ≥25 publications, mỗi bài ≥20 BibTeX đã gán nhãn, tổng ≥500 entries |
| Auto pool | ≥max(ceil(0.1 × (N − M)), 10) publications; gán nhãn toàn bộ entries mỗi bài |
| Validation | 5 manual + 5 auto publications |
| Test | 5 manual + 5 auto publications |
| Train | Các publications đã chọn còn lại |

`N` là số publications thuộc phạm vi đề giao; `M` là số publications trong manual
pool. Ghi rõ danh sách và cách tính mẫu số. Dữ liệu chưa parse xong cần được báo
thiếu; không tự thu nhỏ N theo số bài đang sẵn sàng. Nếu chưa đủ bài cho split,
báo chưa đủ điều kiện, không giảm quota để chạy nghiệm thu.

Đã cập nhật kế hoạch Notion và các task liên quan theo quota nhóm 5 người.

## 2. Đầu vào M1/M2 và phạm vi triển khai

Đã kiểm tra code local: `common/schemas.py` có `BibtexRow`; các module
`matching/*.py` và `pipeline/match.py` hiện chỉ có integration slot.
Hợp đồng này chưa chứng minh corpus thật đã sẵn sàng.

- M2: đọc bảng đã dedup, qua đường dẫn `bibtex` trong `parsing_manifest.json`;
  layout hiện tại là `tables/<paper_key>/bibtex.json`. Yêu cầu manifest complete
  và validation M2 đạt trước khi đưa bài vào nghiệm thu dữ liệu thật.
- M1: đọc artifact `references` từ checkpoint stage references; layout hiện tại
  là `references/<paper_key>.json`. Kiểm tra status và validation tương ứng;
  file tồn tại đơn lẻ chưa đủ chứng minh producer đã hoàn tất.
- Snapshot đầu vào phải bất biến trong lúc chạy; M3 ghi sang output root riêng.
- Một publication chỉ đưa vào matching một lần sau khi M2 hợp nhất các version.
  Không thêm version vào khóa query để lặp lại entries đã dedup.
- `papers`, `elements`, `hierarchy` giữ schema M1/M2; M3 đọc thêm nếu cần,
  không sửa dữ liệu nguồn. Xem [MS2_CONTRACTS.md](MS2_CONTRACTS.md).

## 3. Quy ước chung

`arxiv_id` là publication đang trích dẫn; `ref_arxiv_id` là bài được trích dẫn.
Dùng `common.utils.normalize_id()` để lấy base ID, bỏ hậu tố version khi tạo
khóa. Dùng `paper_key()` riêng cho đường dẫn; không dùng tên thư mục làm ID.

| Tên khóa | Thành phần |
| --- | --- |
| PublicationKey | `arxiv_id` |
| QueryKey | `(arxiv_id, citation_key)` |
| ReferenceKey | `(arxiv_id, ref_arxiv_id)` |
| PairKey | `(arxiv_id, citation_key, ref_arxiv_id)` |

ID và citation key bắt buộc không rỗng. Giữ nguyên hoa/thường citation key của
M2. Không ghép khóa bằng dấu phân cách tùy ý; dùng tuple trong Python và các
cột riêng trong bảng. Join theo khóa, không theo số thứ tự hàng.

JSON dùng UTF-8; scalar thiếu dùng `null`, danh sách thiếu dùng `[]`.
Title thiếu giữ `""` theo M2. Không dùng `"None"`, `"null"`, `"unknown"` làm ID.
Không serialize NaN/Infinity. Kiểu int không nhận bool.

## 4. Các cấu trúc dữ liệu

Kiểu Python dưới đây ánh xạ sang Arrow: `str → string`, `list[str] → list<string>`,
`int → int64`, `float → float64`, `bool → bool`. Nullability là một phần contract.

| Cấu trúc | Trường và kiểu | Khóa / lưu ý |
| --- | --- | --- |
| BibtexInput | `arxiv_id: str`, `citation_key: str`, `entry_type: str`, `title: str`, `authors: list[str]`, `year: int\|None`, `doi: str\|None`, `journal: str\|None`, `url: str\|None` | QueryKey; dùng lại BibtexRow, không tạo schema thứ hai lệch M2 |
| ReferenceInput | `arxiv_id: str`, `ref_arxiv_id: str`, `ref_title: str`, `ref_authors: list[str]`, `ref_submission_date: str\|None`, `ref_s2_id: str\|None` | ReferenceKey; title null của M1 đổi thành chuỗi rỗng trong adapter, có issue |
| PairFeature | Ba cột PairKey và các feature numeric/bool đã đăng ký | Một hàng mỗi pair; không chứa label/provenance làm feature |
| LabelRecord | `arxiv_id: str`, `citation_key: str`, `groundtruth_arxiv_id: str\|None`, `decision: str`, `label_source: str`, `confidence: float\|None`, `annotator: str\|None`, `reviewer: str\|None`, `review_status: str`, `heuristic_version: str\|None`, `notes: str\|None` | QueryKey; một quyết định mỗi query |
| ScoreRecord | Ba cột PairKey và `score: float` | Score hữu hạn, càng lớn càng tốt; không mặc định là probability |
| RankedPrediction | `arxiv_id: str`, `partition: str`, `citation_key: str`, `groundtruth_arxiv_id: str\|None`, `cand_1: str\|None`, `cand_2: str\|None`, `cand_3: str\|None`, `cand_4: str\|None`, `cand_5: str\|None` | QueryKey; schema bảng predictions |
| PartitionRow | `arxiv_id: str`, `partition: str`, `pool: str` | PublicationKey; partition ∈ train/valid/test, pool ∈ manual/auto |

`PartitionManifest` là object chứa `schema_version`, `seed`, `group_size`,
`population_snapshot_id`, `label_snapshot_id`, `manual_publication_ids`,
`auto_publication_ids`, `rows: list[PartitionRow]`, `counts` và `split_id`.
`split_id` là hash nội dung chuẩn hóa, không chỉ là seed. Hai pool tách biệt;
mỗi publication có đúng một partition. Lưu danh sách IDs, không chỉ lưu số lượng.

References không có arXiv ID được giữ nguyên ở nguồn và ghi số lượng loại khỏi
candidate pool trong issues. Không dùng S2 ID thay cho arXiv ID.
Trùng ReferenceKey có metadata giống nhau thì gom một hàng, giữ provenance;
metadata mâu thuẫn phải báo issue và chặn publication để review.
Trùng QueryKey sau M2 là lỗi dữ liệu, không âm thầm chọn hàng đầu.

ReferenceInput hiện không có DOI hoặc năm xuất bản riêng. Feature DOI/arXiv
extracted chỉ được thêm khi có nguồn rõ ràng và thay đổi contract tương ứng.
Nếu suy năm từ `ref_submission_date`, đặt tên `ref_submission_year`; không
coi năm nộp arXiv luôn là năm xuất bản BibTeX.

## 5. Nhãn, candidates và query rỗng

`decision ∈ {matched, unmatched, uncertain}`; `label_source ∈ {manual, heuristic}`;
`review_status ∈ {pending, accepted, rejected}`.

- `matched`: groundtruth khác null, thuộc ReferenceKey của cùng publication.
- `unmatched`: groundtruth null, đã kết luận không có reference phù hợp.
- `uncertain`: groundtruth null, chưa kết luận; không chuyển thành negative.
- Query chưa xử lý chưa có LabelRecord. Chỉ dùng quyết định matched/unmatched
  có review_status accepted cho supervised learning và evaluation.
- Manual confidence có thể null; heuristic confidence bắt buộc trong [0,1],
  có heuristic_version. Confidence là độ tin cậy heuristic, không phải nhãn y.
- Auto abstain phải được giải quyết/review trước khi tính publication đạt quota
  gán nhãn toàn bộ. Manual quota đếm entries có quyết định hợp lệ.

Giữ bảng query riêng từ BibtexInput. Candidate generator tạo Cartesian m×n
trong từng publication, một hàng cho mỗi PairKey duy nhất. Không dùng blocking
hoặc negative sampling cho không gian đánh giá mặc định.

Với query đã có nhãn hợp lệ: `y=1` khi ref_arxiv_id bằng groundtruth, các pair
còn lại `y=0`; unmatched cho toàn 0. Nhiều query có thể cùng trỏ một reference;
không áp đặt matching một-một giữa các query.

Nếu n=0, query vẫn tồn tại nhưng không có pair/score. Prediction có năm cand
null. Không được mất query khi inner join với pairs. Chỉ coi references rỗng là
kết quả hợp lệ khi có bằng chứng upstream; thiếu file/API lỗi là trạng thái khác.

## 6. Giao tiếp giữa các module

Chạy cùng tiến trình Python, truyền typed records hoặc artifact references;
không cần HTTP API/message broker. Mỗi module chịu trách nhiệm kiểm tra input
contract và công bố artifact đã ghi xong. Chữ ký sau là đích triển khai đề xuất.

```python
# TV1: roots được truyền trong context, không đoán đường dẫn giữa M1/M2/M3.
load_publication(arxiv_id, context) -> PublicationBundle
# Bundle: arxiv_id, bibtex, references, issues, readiness.
# readiness: ready / not_ready / invalid; đây là trạng thái bundle, không phải StageResult.

# TV2: giữ original trong bundle; normalized fields nằm ở artifact nội bộ riêng.
clean_publication(bundle, policy) -> CleanPublication
build_pairs(clean_publications) -> list[CandidatePair]
fit_feature_transformer(train_pairs, feature_config) -> FeatureTransformer
transform_pairs(pairs, transformer) -> FeatureBatch
baseline_score(feature_batch, config) -> list[ScoreRecord]

# TV3: candidates luôn đi cùng query registry để không mất query n=0.
auto_label(queries, candidates, policy) -> list[LabelRecord]
validate_labels(queries, candidates, labels) -> ValidationReport

# TV5: split trước mọi phép fit có học từ dữ liệu.
create_split(label_manifest, population_manifest, *, seed, group_size) -> PartitionManifest

# TV4: artifacts chứa model + feature transformer + metadata.
fit_model(train_features, train_targets, valid_features, valid_targets, config) -> ModelArtifact
predict_scores(model_artifact, feature_batch) -> list[ScoreRecord]

# TV5: thứ tự inputs độc lập; nối qua khóa.
build_predictions(queries, candidates, scores, labels, partition_manifest) -> list[RankedPrediction]
evaluate(predictions, labels, partition_manifest, *, partition) -> EvaluationReport
export_dataset(source_tables, predictions, output_dir) -> ExportManifest

# TV1: pipeline cấp tập dữ liệu, khác adapter một publication.
run_matching(config) -> RunManifest
```

`CandidatePair` gồm PairKey và hai record đã clean, chỉ dùng nội bộ.
`FeatureBatch` gồm các hàng PairFeature và `feature_schema_id`.
`FeatureTransformer` lưu vocab/IDF và các tham số cần fit.
`ValidationReport` chứa `valid`, `errors`, `warnings`, counts và query keys bị lỗi.
Các type chưa có trong code phải được khai báo tại `matching/contracts.py`
khi nhóm chấp thuận; không ngầm phụ thuộc vào class riêng của từng TV.

Luồng thực thi:

```text
TV1 load M1/M2 → TV2 clean + candidates → TV3 labels + validation
                                           ↓
                                    TV5 publication split
                                           ↓
TV2 fit features trên train → transform train/valid/test
                                           ↓
TV4 train + chọn model trên valid → khóa cấu hình → score test
                                           ↓
TV5 rank + MRR@5 + export; TV1 điều phối và ghi manifest
```

Cleaning cố định được chạy trước split. TF-IDF, imputation, scaling và mọi phép
fit từ corpus chỉ học trên train; dùng lại transformer đó trên valid/test.
TV2 bàn giao thứ tự feature cố định qua `feature_schema.json`: name, dtype,
nullable, range, missing policy, rationale và source fields. Model kiểm schema
ID trước inference. Không truyền ID, label_source, confidence hay groundtruth
vào X. TV4 có thể sampling negatives trong train; valid/test giữ đầy đủ candidates.

TV4 chỉ nhận train/valid targets để fit/tune. Scoring test không nhận nhãn test;
TV5 giữ nhãn test để đánh giá sau khi khóa model và feature config.

Adapter `run(record, output_dir, *, version=None, upstream=None)` hiện tại là
seam tương thích của repo. TV1 sẽ bọc API nghiệp vụ, không yêu cầu từng TV tạo
một orchestrator riêng. Adapter cấp publication trả StageResult với version=None.
Training/split/evaluation toàn tập dùng manifest cấp run, không giả arxiv_id.

## 7. Ranking và xuất kết quả

Sắp score giảm dần; hòa score thì ref_arxiv_id tăng dần theo chuỗi canonical.
Mỗi candidate xuất hiện một lần, thuộc cùng publication; điền null phía cuối
khi có ít hơn năm references. Thiếu score của pair dự kiến, duplicate score
hoặc score không hữu hạn là lỗi, không thay bằng 0.

MRR@5 tính theo query test đã gán nhãn; no-hit và unmatched nhận 0, vẫn nằm
trong mẫu số. Tập query rỗng báo lỗi, không công bố MRR=0 cho tập test rỗng.
Ví dụ hai query test: một query có đáp án ở hạng 2, query còn lại unmatched,
MRR@5 = (1/2 + 0)/2 = 0.25. Đây là ví dụ giả lập, không phải kết quả thực nghiệm.

Predictions phủ mọi BibTeX entry của publication trong split. Entry
chưa được gán nhãn vẫn được rank, groundtruth null; bảng labels nội bộ phân
biệt nó với unmatched. Evaluator chỉ chọn query có nhãn hợp lệ, không suy nhãn
từ null trong predictions.

Predictions export chỉ có các cột RankedPrediction, partition dùng dictionary
encoding; provenance đặt ở sidecar. Các bảng công bố giữ cột phẳng, primitive
và list một cấp theo schema nguồn. TV5 kiểm tra unique keys, FK, dtype/nullability
và đọc lại Parquet trước bàn giao.

## 8. Artifact, lỗi và khả năng chạy lại

Header manifest M3: `schema_version="3.0"`, `run_id`, `source_version`
(Git commit của code), `ms1_snapshot_id`, `ms2_snapshot_id`, `seed`, config hash,
module versions, feature_schema_id, label_snapshot_id và split_id khi đã có.
`source_version` khác phiên bản LaTeX. Artifact cấp publication có arxiv_id;
artifact toàn tập liệt kê publication IDs. Hash nội dung từng artifact trong
manifest; bảng hàng dữ liệu không lặp header ở từng dòng.

Layout đề xuất, đường dẫn tương đối output root:

```text
manifests/run.json
manifests/partitions.json
manifests/feature_schema.json
publications/<paper_key>/input_manifest.json
publications/<paper_key>/queries.jsonl
publications/<paper_key>/references.jsonl
publications/<paper_key>/issues.jsonl
labels/labels.jsonl
features/<partition>/*.parquet
models/<model_id>/model.joblib
models/<model_id>/metadata.json
scores/<partition>/*.parquet
predictions/<partition>/*.parquet
metrics/evaluation.json
state/stages/<stage>/<paper_key>/paper.json
```

JSONL/Parquet nội bộ là lựa chọn đề xuất; fixture nhỏ có thể dùng JSON.
Model artifact đóng gói transformer và đúng feature schema đã dùng khi train.
Issue có `arxiv_id`, `stage`, `code`, `severity`, `message`, `citation_key|null`,
`ref_arxiv_id|null`, `source_path|null`. Thiếu title là warning; ID sai,
groundtruth ngoài candidates, manifest không khớp là error.

Không đổi SCHEMA_VERSION=1.0 hoặc PARSING_SCHEMA_VERSION=2.0 của M1/M2.
StageResult M3 truyền schema_version tường minh. Giữ các stage statuses sẵn có:
success, no_data, data_error, error, skipped, not_implemented; dùng ErrorInfo
cho lỗi. Bundle not_ready ánh xạ skipped kèm nguyên nhân; invalid ánh xạ data_error.
Không biến missing input thành empty success. Khối chưa bàn giao báo
not_implemented; mock chỉ chạy khi chọn chế độ fixture rõ ràng.

Ghi atomic, không dùng artifact đang ghi dở. Path serialize dùng `/`, tương đối
và không thoát root qua `..` hoặc symlink. Resume chỉ dùng lại artifact khi hash
input, code/config, contract và dependencies còn khớp. Thay labels/split/features
phải làm mất hiệu lực model và các đầu ra phụ thuộc. RunManifest lưu cả lần tạo
artifact gốc và run đang reuse để không gán sai provenance.

Lỗi một publication được ghi riêng và tiếp tục thu thập lỗi các bài khác.
Nếu bài đã thuộc split cố định bị lỗi, run phải incomplete; không tự bỏ bài đó
khỏi test/train hoặc đổi split để báo thành công. Không xuất bản khi còn lỗi chặn.

## 9. Bàn giao contract 3.0

| Người review | Nội dung cần xác nhận | Trạng thái |
| --- | --- | --- |
| TV2 | Candidate keys, ReferenceInput thiếu DOI, feature schema, transformer train-only | Nhóm đã chốt theo xác nhận người dùng |
| TV3 | Quy tắc uncertain/unmatched, label provenance, quota cập nhật | Nhóm đã chốt theo xác nhận người dùng |
| TV4 | FeatureBatch, targets, score có khóa, model/transformer artifact | Nhóm đã chốt theo xác nhận người dùng |
| TV5 | Split theo nhóm 5 người, coverage predictions, MRR và export | Nhóm đã chốt theo xác nhận người dùng |
| TV1 | Mapping input M1/M2, readiness, paths, schema version, orchestration | Đã triển khai phần schema/validators/fixtures |

Fixture contract cần ít nhất ba publications giả lập: exact match, ambiguous
title, unmatched. Bổ sung case references rỗng, query thiếu nhãn, duplicate ID,
cùng citation_key ở hai publications, ID legacy và versioned ID. Fixture phải
được đánh dấu synthetic; ba bài này chỉ kiểm giao tiếp, không đáp ứng quota
manual/auto hoặc split nghiệm thu.

Đầu ra đã triển khai:

- `common/schemas.py`: thêm MATCHING_SCHEMA_VERSION=3.0; giữ default M1=1.0,
  parsing=2.0. BibtexInput dùng lại chính BibtexRow của M2.
- `matching/contracts.py`: ReferenceInput, PublicationBundle, Issue, LabelRecord,
  PairFeature, FeatureSpec/Schema/Batch, ScoreRecord, RankedPrediction,
  PartitionRow/Manifest, ValidationReport và typed key aliases.
- Boundary in-memory `publication_from_rows()` chuẩn hóa ID, kiểm M1/M2 rows,
  ghi cảnh báo dữ liệu thiếu, gom reference trùng nhất quán và chặn xung đột.
- Validators: `validate_labels`, `validate_features`, `validate_scores`,
  `validate_predictions`, `validate_split_quotas`. Constructors chặn sai kiểu/
  giá trị cục bộ bằng ContractError; validators trả ValidationReport cho lỗi
  quan hệ giữa các hàng. Input cấu hình sai/empty không có publication để gắn
  lỗi có thể raise ContractError. Chỉ dùng kết quả khi report.valid là true.
- `tests/fixtures/matching/examples.py`: dữ liệu synthetic tái lập, snapshot
  writer không ghi đè thư mục cũ. Chi tiết tại README trong cùng thư mục.
- `tests/matching/test_contracts.py`: kiểm tương thích serializer M1 thật,
  schema M2, checkpoint versions và các trường hợp dữ liệu biên.

PairFeature.values là mapping nội bộ để TV2 đăng ký đặc trưng; to_dict() xuất
các cột phẳng. FeatureSpec quy định thứ tự/dtype/range/nullability và nguồn;
feature schema ID thay đổi khi thứ tự cột hoặc đặc tả đổi. PartitionManifest
tính counts và split_id từ nội dung, không nhận số liệu tự khai làm bằng chứng.
validate_split_quotas kiểm quota publication; quota entries/annotator vẫn cần
audit nhãn thật của TV3. Fixture 25 manual +12 auto chỉ thử chia tập, không
phải bằng chứng đã gán nhãn 500 entries.

Chưa triển khai trong task này: loader file/checkpoint `matching/io.py`, cleaning,
candidate generation, feature transformer/model artifacts, train/rank/evaluate/
export nghiệp vụ, orchestration/CLI/resume. Không thay các integration slots
bằng success giả. Quy định path, snapshot readiness và resume ở trên sẽ được
áp dụng khi triển khai loader/orchestrator.

Chạy từ repo root: `python -m pytest tests/matching/test_contracts.py -q`.

Changelog: `3.0` (11/11/2026), nhóm chốt; triển khai schema/validators/fixtures.
`3.0-draft.1` (11/11/2026), bản đề xuất đầu tiên và điều chỉnh quota theo đề.
