# T3-05: chọn pool và chuẩn bị bundle

Nhánh `feature/m3-tv3-auto-labeling` thực hiện ba bước đầu. Mẫu số đã chốt là
16.321 bài MS2 `success` và `complete=true`; 25 bài manual được loại, còn 16.296
bài đủ điều kiện. Quota tối thiểu là `ceil(0.1 * 16296) = 1630` bài.
Chọn trước 2.000 bài (400 bài/thành viên) và 150 ID dự phòng để thay bài
không ready. Manifest có các key `publication_ids`, `population_count`,
`population_snapshot_id` theo contract của `split.py`. File
`exclusion_report.json` ghi lý do 8.679 ID không vào population.

## Đầu vào

- `population_manifest.json` trong gói T3-05, gồm `publication_ids` và
  `excluded_manual_ids`. File dữ liệu thật nằm ngoài Git.
- Năm thư mục MS1 `data/` và năm thư mục MS2 `tv1`–`tv5` chứa
  `MS2_RESULTS.zip`. Không cần giải nén. Notebook tự tạo `roots.json` nếu
  hai folder `arxiv_ms1_data` và `arxiv_ms2_result` đã có trong MyDrive.
  Có thể tự cấp đường dẫn khác trong `roots.json` dạng:

```json
{
  "tv1": {"ms1_root": "/content/drive/MyDrive/arxiv_ms1_data/tv1/data", "ms2_root": "/content/drive/MyDrive/arxiv_ms2_result/tv1"},
  "tv2": {"ms1_root": ".../tv2/data", "ms2_root": ".../tv2"},
  "tv3": {"ms1_root": ".../tv3/data", "ms2_root": ".../tv3"},
  "tv4": {"ms1_root": ".../tv4/data", "ms2_root": ".../tv4"},
  "tv5": {"ms1_root": ".../tv5/data", "ms2_root": ".../tv5"}
}
```

`ms2_root` có thể là folder chứa đúng một ZIP, đường dẫn trực tiếp tới ZIP,
hoặc folder đã có `state/` và `tables/`. MS1 root phải có `state/` và
`references/`. Với dữ liệu ở mục **Được chia sẻ với tôi**, cần thêm shortcut
vào MyDrive để Colab thấy folder; chỉ dán URL Drive không tạo đường dẫn file.
Input được đọc, không sửa.

## Chạy

Mở `colab/MS3_T3_05_Silver_Pool_Prepare_CPU.ipynb` trên Colab CPU và chạy ba cell.
Cell 2 chọn 2.000 bài, cân bằng 400 bài/thành viên bằng SHA-256 của seed và ID,
thêm 30 ID dự phòng/thành viên. Cùng manifest và seed cho cùng kết quả. Cell 3
gọi `matching.io.load_with_provenance` để xác thực checkpoint, QA và hash nguồn.
Mỗi bundle ready được lưu riêng cùng receipt; chạy lại sẽ bỏ qua bundle có hash
khớp. `prepared_manifest.json` ghi bài ready và not_ready.

CLI tương đương:

```text
python scripts/silver_pool.py select --population population_manifest.json --ranges configs/ms1_group6_ranges.json --output selection.json --seed 42 --target-count 2000
python scripts/silver_pool.py prepare --selection selection.json --roots roots.json --output prepared
```

Nếu có bài `not_ready`, xem `issues` trong manifest rồi sửa đầu vào hoặc ghi lý do
thay bằng ID dự phòng. Không đổi population/quota hay tự coi bundle lỗi là ready.
Giai đoạn này chưa gán nhãn; kết quả là đầu vào cho các bước chạy heuristic và QA.
