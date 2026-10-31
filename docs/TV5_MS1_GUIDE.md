# Chạy phần TV5 Milestone 1

Chỉ phụ trách crawling QA/benchmark/report; không sửa module MS2. Code ở `reporting/milestone1/`; validator vẫn ở `export/validation.py`.

## Cài đặt và test

Python 3.10 trở lên, tại root repository:

```powershell
python -m pip install -e ".[reporting,dev]"
python -m unittest discover -s tests -v
python -m pytest -q
```

## Benchmark demo offline

```powershell
python -m arxiv_pipeline.reporting.milestone1.benchmark --output demo-output/ms1 --results artifacts/ms1-demo --demo
```

Demo dùng transport giả lập; downloader, extractor, metadata, reference storage và validator thật chạy trên fixtures. Có một ID cố ý không tồn tại nên exit code **1** là kết quả mong đợi của demo. Không coi thời gian/RAM này là kết quả corpus hoặc Colab. Mỗi `--results` phải là thư mục mới; không dùng lại kết quả cũ. Chạy lại thì chọn tên mới. Không xóa dữ liệu trước đó bằng lệnh tự động.

## Benchmark thật và toàn bộ range nhóm

```powershell
python -m arxiv_pipeline.reporting.milestone1.benchmark --output data/ms1-smoke --results artifacts/ms1-smoke -- --ids 2508.12872
python -m arxiv_pipeline.reporting.milestone1.benchmark --output data/ms1-group6 --results artifacts/ms1-group6 -- --range 2508.12872:2508.17871 --range 2506.15788:2506.20787 --range 2411.08799:2411.13798 --range 2410.18025:2410.23024 --range 2410.03025:2410.08024
```

Cấu hình nguồn từ Excel ở `configs/ms1_group6_ranges.json`: 25.000 ID đầu vào thuộc nhóm 6. Một ID chưa chắc tồn tại; version count chỉ biết sau discovery. Notebook Colab đi kèm repository báo cáo có chế độ smoke/full. Benchmark cuối phải chọn CPU-only. Đặt `S2_API_KEY` qua môi trường/Colab Secrets; không ghi key vào notebook hay commit.

Để resume sau ngắt, giữ nguyên output và thêm `--resume` sau dấu `--`, dùng thư mục results mới. Không trộn số đo resumed với fresh. `--force` hiện chưa buộc downloader bỏ cache nội bộ, do đó để đo fresh phải dùng output mới/rỗng. Config timeout/retry/delay của CLI hiện chủ yếu điều khiển discovery; downloader có cấu hình riêng trong code.

## Kết quả và công thức

Mỗi thư mục results có `benchmark.json`, `run.json`, `samples.jsonl`, `timings.jsonl`, `pipeline.log` và `statistics.json` nếu có manifest. Exit khác 0 vẫn giữ bằng chứng lỗi. `benchmark.json` ghi environment/packages, commit và dirty flag, argv không chứa key. Không lưu giá trị biến môi trường. Nếu source dirty, commit chưa tái hiện đủ code: phải commit/review hoặc lưu diff cùng hồ sơ chạy nội bộ trước benchmark cuối.

- Runtime toàn luồng = wall time tiến trình pipeline, bao gồm retry, chờ mạng, pacing, validation và ghi dữ liệu. Thời gian setup gói của môi trường không thuộc runtime pipeline.
- Thời gian paper bao gồm discovery và các version/module của input; alias cùng ID được cộng khi tính trung bình paper trong timing report. Phạm vi input lỗi cũng được ghi. Tổng stage/discovery không bằng toàn wall time vì còn overhead/import/tổng hợp.
- RAM = tổng RSS của tiến trình pipeline và các hậu duệ quan sát được; có thể đếm lại shared pages. Mean là trung bình có trọng số theo thời gian dùng giá trị giữ từ mẫu trước. Chu kỳ mặc định 0,1 giây. Child quá ngắn có thể không được quan sát.
- Disk = tổng logical bytes trong output, bao gồm source, corpus, sidecar, state, logs và staging. Sampler/results nằm ngoài output. Peak là cực đại quan sát, final là dung lượng sau khi tiến trình kết thúc; không phải dung lượng ổ đĩa toàn máy hay allocated filesystem blocks.
- Download/extract success = số checkpoint thành công / số version discovery thành công đã biết. Checkpoint thiếu vẫn nằm trong mẫu số. Paper discovery lỗi có version count chưa biết, được báo riêng.
- Source-complete paper = mọi version có download/extract success / số paper ID được chọn. Đây là trạng thái xử lý, cần xem thêm `validation_pass` về toàn vẹn.
- Reference query success = số paper có dữ liệu JSON đọc được ở status success/no_data / số paper discovery thành công. Báo riêng nonempty success và `not_indexed`; trung bình số refs chỉ trên paper truy vấn được (gồm no_data=0).
- Reference metadata completeness = số reference có đủ năm trường ref_arxiv_id/title/authors/submission_date/s2_id / tổng reference truy vấn được. Không có arXiv ID vẫn giữ trong dữ liệu nội bộ, tính là thiếu với chỉ số completeness này.
- Kích thước trước/sau lọc = so sánh các cặp số đo **đã giải nén** trong extract sidecar, tổng mọi version mỗi paper, chỉ lấy paper có đủ cặp số liệu. Kích thước package nén báo riêng, không dùng suy ra tỷ lệ loại hình.
- Không có mẫu số thì tỷ lệ là `null`, không phải 0%. Manifest giới hạn paper được chọn; không quét toàn cache để đếm lẫn các lượt chạy.

## Kiểm tra corpus có sẵn và xuất báo cáo

```powershell
python -m arxiv_pipeline.reporting.milestone1.statistics --output data/ms1-group6 --save artifacts/ms1-checked.json --revalidate
```

Không chạy cùng lúc với tiến trình đang ghi corpus. `--revalidate` cập nhật JSON validation nhưng không sửa checkpoint validate/summary cũ. `--resume` có thể reuse validation cũ, không bảo đảm tái kiểm nội dung. Sau ngắt, manifest có thể là snapshot cũ; resume để hoàn tất snapshot trước khi tổng hợp, không trình bày manifest cũ như kết quả lượt đang dở.

Sao chép `benchmark.json`, `statistics.json`, `samples.jsonl`, `timings.jsonl` sang `summaries/milestone1/<run-name>/` của repository báo cáo; không sao chép corpus hoặc secrets. Đặt `TV5_RUN` theo tên run khi build. Xem README của report để build và deploy.

## Bản sửa sau review

- Mỗi lượt benchmark có UUID `run_id`; pipeline ghi `manifests/run.json` sau khi hoàn tất snapshot, kèm SHA-256 của discovery, stages và summary. Wrapper chỉ xuất `statistics.json` khi receipt và hashes khớp lượt hiện tại. Exit 1 vì input/stage lỗi vẫn có thể là snapshot hoàn tất; không dựa riêng vào exit code để loại thống kê.
- Nếu bị ngắt hoặc CLI lỗi trước snapshot, `benchmark.json` vẫn được lưu, `statistics_status=unavailable_current_run`, và có `statistics_error.json`. Không xuất dữ liệu từ manifest cũ.
- Timing JSONL sai schema/ghi dở được ghi vào `timings.diagnostics`; vẫn tổng hợp các event hợp lệ và giữ báo cáo tài nguyên.
- Extraction `skipped` vì download `no_source` là outcome hợp lệ, không nằm trong bảng nguyên nhân lỗi. Skip vì lỗi upstream vẫn là lỗi.
- Cleanup dọn cả các child đã theo dõi, kể cả khi parent đã thoát; báo `cleanup.remaining_pids` nếu không dọn được hoàn toàn.

Kiểm tra toàn assignment mà không cần crawl lại:

```powershell
python -m arxiv_pipeline.reporting.milestone1.audit --output data/ms1-group6 --assignment configs/ms1_group6_ranges.json --save artifacts/ms1-assignment-audit.json
```

Audit đối chiếu toàn bộ ID được giao với checkpoints/manifest hiện có, chạy validator trên các paper discovery success và báo `unseen_ids`, `unresolved_discovery_ids`. Không có record không có nghĩa ID không tồn tại: nó chỉ chưa được xử lý. File Excel là danh sách đầu vào, không phải corpus. Audit exit 1 khi chưa đủ dữ liệu hoặc có lỗi; `not_found` chỉ được xem là lookup đã giải quyết, không phải paper tải thành công.

## Nguồn tham khảo

Mẫu đã chạy trên Google Colab CPU-only ngày 28/10/2026: commit source sạch `0f8d83b591c89cf085f344f5fb59af671ab43b81`, ID `2508.12872`, output mới, exit 0, một version, 97 references, validator pass. Wall 21,511 giây, peak RSS quan sát 41.771.008 bytes. Report run `colab-cpu-sample` giữ số đo và provenance; archive gốc ở `artifacts/TV5_MS1_COLAB_20261028T150644Z.zip` (Git ignored). Audit cả 25.000 ID xác nhận còn 24.999 ID chưa có record. Đây là mẫu Colab, chưa nghiệm thu toàn assignment.

- [Đề chính thức MS1](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/)
- [psutil process API](https://psutil.readthedocs.io/en/latest/#process-class)
- `docs/CONTRACTS.md`, module crawling và báo cáo TV2 là tài liệu nội bộ mô tả triển khai; kết quả hiệu năng phải đo lại.
