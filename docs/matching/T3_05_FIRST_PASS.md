# T3-05: lượt gán nhãn heuristic đầu tiên

Chạy `colab/MS3_T3_05_Auto_Label_First_Pass_CPU.ipynb` sau khi notebook Prepare
đã xuất `output/prepared/prepared_manifest.json`. Notebook dùng bundle đã kiểm tra
hash, không đọc lại ZIP MS2. CLI tương đương:

```text
python scripts/silver_first_pass.py --prepared output/prepared/prepared_manifest.json --population population_manifest.json --output output/first_pass
```

Kết quả trong `output/first_pass/`:

- `draft_labels.json`: mọi BibTeX entry của các bài `ready` có đúng một nhãn
  `LabelRecord`. Các rule rõ ràng có `review_status=accepted`; abstain giữ
  `decision=uncertain`, `review_status=pending`.
- `uncertain_rows.json`: key, rule, confidence và đường dẫn bundle cho các
  entry cần xem lại.
- `first_pass_manifest.json`: tổng nhãn, số bài phủ đủ entries tạm thời,
  số abstain, phân bố rule/quyết định và quota tối thiểu. Báo cáo tách riêng
  số bài phủ đủ **có references**, **không có references** và **có ít nhất một
  matched** để không nhầm nhãn unmatched tầm thường với matching có ứng viên.
- `per_publication/`: nhãn từng bài và receipt có hash để chạy lại không phải
  gán lại bài đã hoàn tất.

`candidate_full_coverage_publications` chỉ là số bài không còn abstain sau
lượt heuristic; **chưa phải kết quả QA hay số bài được bàn giao**. Không đổi
`uncertain` thành `unmatched` để đủ quota. Bước tiếp theo là xử lý abstain,
spot-check nhãn tự động, rồi validate và xuất gói manual + auto cho TV5.
