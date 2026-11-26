# Model card — Reference Matching (MS3)

> Số liệu test để trống cho tới khi khóa mô hình và chấm test. Không điền số từ dữ liệu giả lập.

## 1. Mô hình

| Mục | Giá trị |
|---|---|
| Bài toán | Phân loại cặp (BibTeX, reference) trong cùng publication, xếp hạng candidate theo score; LambdaMART học trực tiếp thứ hạng |
| Mô hình so sánh | Baseline heuristic (`baseline.predict_baseline_scores`), Logistic Regression, Random Forest, HistGradientBoosting, LambdaMART |
| Dữ liệu đầu vào | Feature cặp tính sẵn (`features_{train,valid,test_unlabeled}.parquet`, 3.696 bài) + split và nhãn train/valid (không có nhãn test) |
| Feature | 16 feature của `features.get_feature_schema()` (schema 3.0), thứ tự cột cố định theo schema |
| Nhãn train | Manual + heuristic đã `accepted`; bỏ `uncertain`/`pending` |
| Mất cân bằng lớp | `class_weight="balanced"`; không negative sampling (train trên toàn bộ ma trận) |
| Chọn mô hình | MRR@5 trên valid bằng evaluator chính thức (`metrics.score_mrr_at_5`); hoà điểm giữ cấu hình đứng trước (đơn giản hơn) |
| Mô hình cuối | Logistic Regression, `C=0.01` (`logreg-53571b7d07e5`), khóa lúc 2026-11-26 15:31 UTC |
| MRR@5 valid / test | 0,2214 (bằng trần 91/411) / — (chấm từ `scores/test`) |

## 2. Chống rò rỉ

- Nhãn chỉ đọc từ ZIP bàn giao split; `labels_test_tv5_only.json` và `labels_all_merged.json` bị chặn theo tên,
  và bất kỳ nhãn nào thuộc bài test trong ZIP làm dừng run.
- Split theo publication đã cố định (seed 42); bài valid/test thiếu feature làm dừng run, không bị bỏ âm thầm.
  Bài không có reference arXiv (0 candidate) không có dòng feature; query của chúng vẫn tính 0 điểm trong MRR@5.
- `fit_model` không có tham số test; imputer/scaler nằm trong pipeline, fit trên train.
- Test chỉ chấm qua `model.score_test`, sau `model.freeze` (`frozen.json` ghi model_id, split_id, commit, thời
  điểm, SHA-256 của `model.joblib`). Mỗi lần chấm ghi vào `test_access.jsonl`.

## 3. Tái lập kết quả

Đầu vào đặt trong `data/` (đã nằm trong `.gitignore`):

```bash
pip install -e ".[matching,dev]"
# Train, chọn mô hình trên valid, phân tích lỗi (chưa đụng test)
python -m arxiv_pipeline.matching.runner --handoff <split_handoff.zip> \
    --features <thư mục features> --output data/runs --run-id <RUN_ID>
# Sau khi xem metrics/ và analysis/: khóa mô hình và chấm test (cùng RUN_ID)
python -m arxiv_pipeline.matching.runner --handoff <split_handoff.zip> \
    --features <thư mục features> --output data/runs --run-id <RUN_ID> --freeze
```

Chạy lại cùng `--run-id` bỏ qua các bước đã xong; đổi tham số (kinds, seed, neg_per_query, use_silver) với
RUN_ID cũ bị từ chối. Các ID phải khớp:

| ID | Nằm ở | Giá trị |
|---|---|---|
| `split_id` | `state/inputs.json`, `frozen.json` | `7a08e01a…` |
| `label_snapshot_id` | `state/inputs.json` | `labels-3c50644b9c964edf` |
| `feature_schema_id` | `frozen.json` | `5176b5c594d3…` |
| `model_id` | `metrics/model_selection.json`, `frozen.json` | `logreg-53571b7d07e5` |
| `handoff_sha256` | `state/inputs.json` | — |

`model_id` là hash của cấu hình, dữ liệu, nhãn và code train: cùng đầu vào cho cùng `model_id`.
Dùng lại model: `model.load_artifact("models/<model_id>")` rồi `model.predict_score_array`;
`load_artifact` từ chối nếu khác phiên bản scikit-learn lúc train. Không chạy `--freeze` lại để "thử":
test chỉ được chấm một lần; đổi cấu hình phải tạo RUN_ID mới và khai báo trong report.

## 4. Giới hạn

- Valid bão hoà: 91/411 query có đáp án, mọi mô hình và cả baseline đều xếp đúng 91/91 ở hạng 1, nên MRR@5 valid
  bằng trần 0,2214 và không phân biệt được mô hình; lựa chọn khi đó theo quy tắc hoà điểm.
- Hơn 99% nhãn train là nhãn heuristic dựa trên độ giống tiêu đề, nên mô hình có xu hướng học lại heuristic đó.
- Random Forest dùng `max_samples=0.2` (kích thước bootstrap) để train trên ~3,7 triệu cặp trong thời gian hợp lý.
- MRR@5 train là trên mẫu tối đa 5.000 query train (chỉ để xem overfit), không phải toàn bộ train.
