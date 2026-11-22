# arxiv-data-science

Pipeline xử lý corpus arXiv theo phiên bản (**Version-Aware arXiv Corpus Construction & Reference Matching**) cho đồ án môn *Introduction to Data Science* (HCMUS · Nhóm 6, 5 thành viên: TV1–TV5), bao gồm đầy đủ 3 cột mốc:

- **Milestone 1 — Crawling:** Discovery mọi version từ arXiv API, tải và giải nén mã nguồn LaTeX (`.tex`/`.bib`, loại bỏ hình ảnh nhị phân), chuẩn hóa metadata và thu thập references từ Semantic Scholar API.
- **Milestone 2 — Parsing:** Hợp nhất đường dẫn biên dịch LaTeX (`\input`/`\include`), dựng cây phân cấp phần tử, chuẩn hóa văn bản/công thức, hợp nhất BibTeX và khử trùng lặp đa phiên bản.
- **Milestone 3 — Reference Matching & Final Dataset:** Gán nhãn manual/heuristic, chia tập chống rò rỉ theo publication, trích xuất đặc trưng, huấn luyện/so sánh mô hình xếp hạng, đánh giá **MRR@5**, kiểm định 6 bảng và xuất **sharded Parquet** lên **Hugging Face Hub**.

---

## 1. Cấu trúc Repository

```text
arxiv-data-science/
├── main.py                              # Entrypoint CLI (chạy trực tiếp không cần cài package)
├── pyproject.toml                       # Khai báo package và extras [reporting,matching,export,dev]
├── requirements.txt                     # Cài đặt đầy đủ môi trường tái lập
├── configs/
│   └── ms1_group6_ranges.json           # Dải 25.000 arXiv ID được giao cho nhóm 6
├── colab/                               # Notebook Colab CPU-only cho MS1 & MS2
├── docs/
│   ├── CONTRACTS.md                     # Hợp đồng dữ liệu MS1 (schema 1.0)
│   ├── MS2_CONTRACTS.md                 # Hợp đồng parsing MS2 (schema 2.0)
│   ├── MS2_RESOLVER.md                  # Đặc tả LaTeX resolver
│   ├── MS2_HANDOFF.md                   # Biên bản bàn giao MS2
│   ├── MS3_CONTRACTS.md                 # Hợp đồng matching & export MS3 (schema 3.0)
│   └── matching/annotation_guideline.md # Quy chuẩn gán nhãn thủ công & heuristic (TV3)
├── poster/
│   └── README.md                        # Đặc tả bàn giao metrics/export cho poster A2 portrait
├── scripts/                             # Script hỗ trợ chạy pipeline & đồng bộ notebook
├── src/arxiv_pipeline/
│   ├── common/                          # Config, logger, schemas (1.0/2.0/3.0), canonical ID utils
│   ├── crawling/                        # Discovery, downloader, extractor, metadata, Semantic Scholar
│   ├── parsing/                         # Resolver, hierarchy, normalization, bibliography, deduplication
│   ├── matching/
│   │   ├── contracts.py                 # Typed records & validators MS3 (TV1)
│   │   ├── cleaning.py, features.py     # Làm sạch & đặc trưng cặp BibTeX–Reference (TV2)
│   │   ├── heuristics.py, labeling.py   # Gán nhãn heuristic, CSV/CLI gán nhãn tay & QA (TV3)
│   │   ├── model.py, train.py, analysis.py # Mô hình xếp hạng & phân tích lỗi trên valid (TV4)
│   │   ├── split.py                     # Chia train/valid/test theo publication & leakage guard (TV5)
│   │   ├── metrics.py                   # MRR@5 chính thức, Recall@k, tie/duplicate/missing check (TV5)
│   │   ├── prediction.py                # Xếp hạng Top-5 & dựng bảng predictions (TV5)
│   │   ├── evaluation.py                # Đánh giá TV4 vs baseline TV2, slices & error taxonomy (TV5)
│   │   └── export.py                    # Kiểm định 6 bảng, sharded Parquet & upload HF Hub (TV5)
│   ├── export/                          # Validator MS1 (`validation.py`) & facade Parquet (`parquet.py`)
│   ├── pipeline/                        # Orchestrator CLI cho MS1/MS2/MS3 với checkpoint/resume
│   └── reporting/                       # Benchmark & audit tài nguyên cho MS1/MS2
└── tests/                               # Unit & integration tests offline cho MS1–MS3
```

---

## 2. Thiết lập môi trường

Yêu cầu **Python 3.10+**.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e ".[dev]"
```

Để truy vấn Semantic Scholar nhanh hơn khi chạy MS1, tạo tệp `.env` từ `.env.example` và đặt biến `S2_API_KEY` (không commit `.env`).

---

## 3. Hướng dẫn chạy từng Milestone

### Milestone 1 — Crawling (Discovery, Source, Metadata, References, Validate)

```powershell
# Chạy discovery một hoặc nhiều bài
python main.py --ids 2301.00001 2301.00002 --output data --stage discovery

# Chạy crawl đầy đủ với checkpoint/resume
python main.py --input ids.txt --output data --stage crawl --resume

# Lấy references Semantic Scholar theo batch trước khi chạy crawl --resume
python -m arxiv_pipeline.crawling.s2.batch --input ids.txt --output data
```

- Đầu ra nằm tách biệt trong `--output`: `manifests/`, `state/`, `raw/`, `corpus/`, `metadata/`, `references/`, `validation/`, `summary.json`.
- Tôn trọng độ trễ tối thiểu 3 giây/request; không tải lại các stage terminal đã hoàn tất khi dùng `--resume`; dùng `--force` để chạy lại snapshot mới.
- Benchmark Colab CPU-only xem [colab/MS1_Benchmark_CPU.ipynb](colab/MS1_Benchmark_CPU.ipynb) và [colab/README.md](colab/README.md).

### Milestone 2 — Parsing Offline

```powershell
python main.py --stage parse --ms1-input artifacts/ms1-snapshot --output artifacts/ms2 --resume
```

- Đọc snapshot MS1 bất biến, ghi ra thư mục MS2 tách biệt theo luồng: `Resolver → Hierarchy → Normalization` song song `Resolver → Bibliography`, sau đó `Deduplication → Validate Parse`.
- Chi tiết schema 2.0 và envelope xem [docs/MS2_CONTRACTS.md](docs/MS2_CONTRACTS.md) và [colab/MS2_Integration_CPU.ipynb](colab/MS2_Integration_CPU.ipynb).

### Milestone 3 — Reference Matching, Evaluation & Parquet Export

#### 3.1. Giao thức gán nhãn (Labeling Protocol — TV3)

Chi tiết tại [docs/matching/annotation_guideline.md](docs/matching/annotation_guideline.md) và [docs/MS3_CONTRACTS.md](docs/MS3_CONTRACTS.md):

- **Phạm vi đối chiếu:** Chỉ ghép cặp trong cùng một publication `(arxiv_id, citation_key) ↔ ref_arxiv_id`.
- **3 quyết định (`decision`):** `matched` (`groundtruth_arxiv_id != null`), `unmatched` (`null`, chắc chắn không có arXiv reference tương ứng), và `uncertain` (`null`, cần review chéo — không đưa vào supervised train/eval).
- **Trạng thái duyệt (`review_status`):** Chỉ nhãn có `review_status="accepted"` và `decision != "uncertain"` (`LabelRecord.is_supervised == True`) mới tham gia huấn luyện và tính MRR@5.
- **Định mức nhóm 5 người (`k = 5`):**
  - **Manual pool (Gold):** ≥ 25 publications (mỗi thành viên ≥ 5 publications), mỗi bài ≥ 20 BibTeX entries đã gán nhãn → tổng ≥ 500 entries.
  - **Automatic pool (Silver):** ≥ `max(ceil(0.1 × (N − M)), 10)` publications ngoài manual pool; gán nhãn 100% BibTeX entries của các bài được chọn và kiểm định chất lượng bằng spot-check audit.
- Công cụ gán nhãn tay: `export_annotation_sheet`, `run_interactive_cli`, `load_annotation_sheet`, `audit_label_manifest` trong `src/arxiv_pipeline/matching/labeling.py`.

#### 3.2. Đặc trưng & Mô hình (TV2 & TV4)

- **Đặc trưng cặp (`PairFeature` / `FeatureSchema`):** 4 nhóm đặc trưng (`title`, `author`, `identifier`, `temporal`), mọi transformer học từ corpus (TF-IDF, imputer, scaler) chỉ `fit` trên tập **train** rồi `transform` sang `valid`/`test`. Các cột định danh và metadata nhãn (`arxiv_id`, `citation_key`, `ref_arxiv_id`, `groundtruth_arxiv_id`, `label_source`, `confidence`, `partition`) bị chặn tuyệt đối ở tầng contract.
- **Mô hình (`model.py` / `train.py`):** Hỗ trợ `logreg`, `rf`, `hgb`, `lambdamart`; negative sampling chỉ áp dụng trên `train`; chọn siêu tham số và mô hình tốt nhất chỉ dựa trên **Validation MRR@5**; tập `test` bị khóa hoàn toàn cho tới bước đánh giá cuối của TV5.
- Chạy thử nghiệm mock (chỉ kiểm luồng, **không phải kết quả thực nghiệm**):

```powershell
python -m arxiv_pipeline.matching.train --mock --out artifacts/ms3_mock
python -m arxiv_pipeline.matching.analysis --mock
```

#### 3.3. Chia tập, Đánh giá MRR@5 & Xuất Sharded Parquet (TV5)

1. **Chia tập theo publication (`matching/split.py`):**
   - `create_split(label_manifest, population_manifest, *, seed=42, group_size=5) -> PartitionManifest`
   - Validation: `5 manual + 5 auto`; Test: `5 manual + 5 auto`; Train: phần còn lại.
   - `assert_no_split_leakage(...)` kiểm chứng tập ID rời nhau hoàn toàn giữa `train`/`valid`/`test` và `manual`/`auto`, khớp `label_source`, chống rò rỉ feature/prediction.
2. **Xếp hạng Top-5 & MRR@5 (`matching/prediction.py`, `matching/metrics.py`):**
   - `build_predictions(queries, candidates, scores, labels, partition_manifest) -> list[RankedPrediction]`
   - Sắp điểm giảm dần; hòa điểm phá bằng `ref_arxiv_id` canonical tăng dần; điền `None` ở cuối khi có ít hơn 5 candidates (giữ nguyên cả query có 0 references).
   - **Quy ước MRR@5 chính thức:**
     $$\text{MRR@5} = \frac{1}{|Q|} \sum_{q \in Q} \text{RR@5}(q), \qquad \text{RR@5}(q) = \begin{cases} 1/r & \text{nếu đáp án ở hạng } r \le 5 \\ 0 & \text{nếu miss hoặc } \texttt{unmatched} \end{cases}$$
     Mọi query đã gán nhãn hợp lệ (`matched` và `unmatched`, kể cả 0-candidate) đều nằm trong mẫu số `|Q|`. Report luôn ghi rõ `unmatched_policy="include_zero_in_denominator"` kèm chỉ số chẩn đoán bổ sung (`mrr_at_5_matched_only`, `recall_at_1/3/5`).
3. **Đánh giá TV4 vs Baseline TV2 (`matching/evaluation.py`):**
   - `evaluate_scores(bundles, scores, labels, partition_manifest, *, partition="test", baseline_scores=...)` tạo báo cáo JSON (`allow_nan=False`) gồm metrics, `prediction_preview`, `slices` (pool, metadata, candidate count, per-publication), `confusion_and_errors` và delta so sánh với baseline.
4. **Kiểm định 6 bảng, Sharded Parquet & Hugging Face Hub (`matching/export.py`):**
   - `prepare_published_references(raw_refs)` lọc giữ các reference có `ref_arxiv_id != null` theo đúng quy định xuất bản MS1/MS3.
   - `export_hf_dataset(tables, paths, ...)` kiểm định toàn bộ khóa chính, khóa ngoại liên bảng, kiểu dữ liệu, dictionary encoding và tính toàn vẹn partition; ghi sharded Parquet nguyên tử; tự động đọc ngược (`verify_parquet_export`) kiểm tra schema, tổng số hàng và SHA-256; sinh `export_manifest.json` và `README.md` dataset card YAML chuẩn Hugging Face (`data/<table>/*.parquet`, không dùng loading script tùy chỉnh).
   - `upload_to_hf(...)` **chặn cứng** mọi nỗ lực upload dữ liệu `synthetic=True` hoặc chưa qua read-back verification; `verify_hf_repo_visibility(...)` xác minh repo public và đủ 6 bảng Parquet.

---

## 4. Schema 6 bảng công bố trên Hugging Face

| Bảng | Cột và kiểu Arrow | Ghi chú khóa & ràng buộc |
| --- | --- | --- |
| `papers` | `arxiv_id: string`, `title: string`, `authors: list<string>`, `submission_date: string`, `revised_dates: list<string>`, `venue: string (nullable)` | PK: `arxiv_id` |
| `references` | `arxiv_id: string`, `ref_arxiv_id: string`, `ref_title: string (nullable)`, `ref_authors: list<string>`, `ref_submission_date: string (nullable)`, `ref_s2_id: string (nullable)` | PK: `(arxiv_id, ref_arxiv_id)`, FK → `papers`; chỉ giữ reference có arXiv ID |
| `elements` | `element_id: string`, `arxiv_id: string`, `kind: dictionary<int8, string>`, `content: string` | PK: `element_id`, FK → `papers`; đã dedup xuyên phiên bản |
| `hierarchy` | `arxiv_id: string`, `version: int64`, `element_id: string`, `parent_id: string` | Unique: `(arxiv_id, version, element_id)`, FK → `papers` & `elements` (cùng `arxiv_id`) |
| `bibtex` | `arxiv_id: string`, `citation_key: string`, `entry_type: dictionary<int8, string>`, `title: string`, `authors: list<string>`, `year: int64 (nullable)`, `doi: string (nullable)`, `journal: string (nullable)`, `url: string (nullable)` | PK: `(arxiv_id, citation_key)`, FK → `papers` |
| `predictions` | `arxiv_id: string`, `partition: dictionary<int8, string>`, `citation_key: string`, `groundtruth_arxiv_id: string (nullable)`, `cand_1..cand_5: string (nullable)` | PK: `(arxiv_id, citation_key)`, FK → `bibtex` & `references`; chỉ chứa các bài thuộc `train`/`valid`/`test` |

Cấu trúc repository dataset trên Hugging Face:

```text
<dataset-repo>/
├── README.md
└── data/
    ├── papers/part-*.parquet
    ├── references/part-*.parquet
    ├── elements/part-*.parquet
    ├── hierarchy/part-*.parquet
    ├── bibtex/part-*.parquet
    └── predictions/part-*.parquet
```

---

## 5. Kiểm thử

Toàn bộ test chạy offline, không gọi mạng hay ghi đè dữ liệu thật:

```powershell
# Chạy riêng bộ test Milestone 3 (TV1, TV3, TV4, TV5)
python -m pytest tests/matching/ -v

# Chạy riêng bộ test TV5 (split, MRR@5, predictions, evaluation, 6-table Parquet & HF guards)
python -m pytest tests/matching/test_evaluation.py tests/matching/test_export.py -v

# Chạy toàn bộ test suite (MS1 + MS2 + MS3)
python -m pytest -q
```
