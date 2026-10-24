# arxiv-data-science

Phần bàn giao TV1: cấu trúc project MS1–MS3, Discovery mọi version, hợp đồng dữ liệu v1 và CLI điều phối có checkpoint/resume. Python **3.10+**, runtime TV1 chỉ dùng standard library.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
arxiv-pipeline --ids 2301.00001 2301.00002 --output data --stage discovery
```

Có thể chạy ngay từ checkout, không cần cài package:

```powershell
python main.py --ids 2301.00001 --output data
python main.py --input ids.txt --range 2301.00001:2301.00003 --output data --resume
python scripts/run_pipeline.py --ids hep-th/9901001 --output data
python main.py --ids 2301.00001 --output data --force
```

`ids.txt` dùng UTF-8, mỗi dòng một ID, cho phép dòng trống và comment `#`. `--ids` nhận ID phân cách bằng khoảng trắng hoặc dấu phẩy, URL abs/pdf, tiền tố `arXiv:` và version cụ thể. Range là khoảng đóng trong cùng tháng, tối đa 10.000 ID. Đầu vào version cụ thể vẫn phát hiện toàn bộ version của paper; version không tồn tại được ghi lỗi riêng. ID không hợp lệ không làm dừng các ID còn lại.

`--resume` dùng lại discovery hoàn chỉnh và thử lại các input thất bại. `--force` cập nhật snapshot latest và chạy lại các adapter; không dùng cùng `--resume`. Mặc định không có resume thì chạy mới. `--timeout 30`, `--retries 3`, `--request-delay 3` là mặc định; retries không gồm lần đầu, delay tối thiểu 3 giây. Mỗi output root chỉ có một process ghi tại một thời điểm.

Đầu ra chính là `manifests/discovery.json`, `manifests/stages.json`, `summary.json`, `state/` và `logs/pipeline.log` bên trong output root. Record `success` có đầy đủ v1..vn, metadata ngày từng version và đường dẫn source/corpus cố định. `invalid_id`, `not_found`, `api_error`, `data_error` được tách biệt. Không tải source bằng lệnh discovery.

Hợp đồng cho **TV2–TV5**, schema, đường dẫn và giao diện adapter nằm ở [docs/CONTRACTS.md](docs/CONTRACTS.md). Đây là baseline TV1 để nhóm review; chưa có xác nhận của các thành viên. Các module chưa bàn giao có hàm `run(...)` ném `NotImplementedError` rõ ràng.

```powershell
python main.py --input ids.txt --output data --stage crawl --resume
```

Lệnh crawl kết nối Discovery → download → extract, sau đó metadata → references → validate. Khi adapter chưa được bàn giao, manifest ghi `not_implemented`/`skipped`, exit 1. Không báo thành công toàn pipeline khi mới có Discovery. MS2/MS3 và Parquet là khung chờ triển khai.

Kiểm thử offline (không gọi arXiv, không cần tải dependencies):

```powershell
python -m unittest discover -s tests -v
```

Tests bao gồm HTTP retry, lỗi Atom/data, ID cũ/mới, mọi version, CLI và resume sau lỗi/gián đoạn, tích hợp adapter giả lập. Live arXiv cần Internet; fixtures không chứng minh API đang khả dụng. Exit: 0 hoàn tất, 1 có lỗi/chưa bàn giao, 2 tham số sai, 130 bị ngắt. `no_source`, `not_indexed`, `no_data` là outcome riêng, không dùng để tính success rate của source.

Cấu trúc: `src/arxiv_pipeline/common`, `crawling` (MS1), `parsing` (MS2), `matching` (MS3), `export`, `pipeline`; `scripts/run_pipeline.py`; tests tương ứng. Đầu vào/đầu ra lớn được gitignore; không commit corpus.

Quy trình nhóm: tạo issue có người phụ trách, input/output và tiêu chí nghiệm thu; làm trên nhánh `feature/...` theo kế hoạch nhóm, PR có hướng dẫn test và ít nhất một reviewer, không commit trực tiếp main. Việc merge nhánh thành viên, tạo repository báo cáo và benchmark Colab thực hiện khi có module/quyền truy cập tương ứng.
