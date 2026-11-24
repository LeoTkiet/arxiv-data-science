# Poster Metrics & Export Handoff (Milestone 3 · TV5)

Thư mục này chứa tài liệu bàn giao số liệu đầu ra từ `evaluation.py` và `export.py` phục vụ thiết kế poster **1 slide A2 portrait** cho phiên Poster Session cuối kỳ.

## Nguồn số liệu chính thức (tuyệt đối không dùng số synthetic/mock)

1. **Split & Leakage (`manifests/partitions.json`)**:
   - Số publications theo `train / valid / test` tách riêng hai pool `manual` và `auto` (nhóm 5 người: Valid = 5M + 5A, Test = 5M + 5A, Train = phần còn lại).
2. **Evaluation & Comparison (`metrics/evaluation_valid.json`, `metrics/evaluation_test.json`)**:
   - `model.metrics.mrr_at_5` (MRR@5 chính thức, bao gồm `unmatched` = `0.0` trong mẫu số `|Q|`), `mrr_at_5_matched_only`, `recall_at_1`, `recall_at_3`, `recall_at_5`.
   - `comparison`: delta giữa mô hình TV4 và baseline TV2 + thống kê query-level (`improved`, `regressed`, `unchanged`).
   - `model.slices`: theo `by_pool` (`manual` vs `auto`), `by_metadata`, `by_candidate_count`, `by_publication`.
   - `model.confusion_and_errors`: `top1_confusion`, `error_taxonomy` và các ví dụ lỗi điển hình.
3. **Published Dataset (`hf_dataset/export_manifest.json`)**:
   - Tổng số dòng, số shard Parquet và dung lượng của 6 bảng (`papers`, `references`, `elements`, `hierarchy`, `bibtex`, `predictions`) kèm đường dẫn public Hugging Face dataset sau khi kiểm tra `verify_hf_repo_visibility(...)`.
