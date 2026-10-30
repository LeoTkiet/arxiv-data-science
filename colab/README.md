# Notebook MS1 CPU-only

Mở `MS1_Benchmark_CPU.ipynb` trên Colab, chọn CPU, chạy setup thành công rồi chọn phạm vi. Source cố định tại commit đã đo/kiểm chứng `4ea44c42810f3bb1564080c19d1ed889f3498ee0`. Không tự đổi sang `main` hay bỏ qua test lỗi.

- **Cơ chế Smart Fast Resume**: Khi bật `AUTO_RESUME=True` và `RESUME_START_ID='AUTO'`, notebook tự động quét số bài đã hoàn tất (trong `validation` và `raw`), lùi lại 5 bài đệm để tiếp tục tải ngay lập tức, tránh việc quét lại hàng ngàn bài cũ trên Google Drive.
- **Tăng tốc Gom lô (Batch Pre-fetch)**: Bật `FAST_BATCH_PREFETCH=True` để tự động chạy gom lô Metadata arXiv (`discovery_batch`) và gom lô trích dẫn Semantic Scholar (`s2.batch`) trong 50–100 IDs/request. Bước này giúp hoàn thành trước toàn bộ metadata và trích dẫn chỉ trong 4 phút, giảm hơn 6.5 giờ chờ mạng.
- **Thanh tiến trình thời gian thực**: Cell 6 hiển thị trực tiếp số bài đã xong, mã bài hiện tại, tốc độ (s/bài) và thời gian dự kiến (ETA) theo thời gian thực thay vì màn hình đứng im.

- Mỗi lần chạy cell benchmark có UUID riêng trong tên thư mục kết quả. Không xóa kết quả cũ; output corpus/checkpoint giữ nguyên để resume. Tên ZIP cũng chứa tên run duy nhất.
- Audit cá nhân dùng đúng dải đã chọn, không dùng cả 25.000 ID làm mẫu số cho một thành viên. Smoke chỉ audit một ID. Range là lookup inputs, không bảo đảm có từng ấy publication.
- `assignment-audit.json` là audit của phạm vi hiện tại. Đọc `unseen_ids`, `unresolved_discovery_ids`, `discovered_assigned_ids`, `validation_pass_papers`, `validation_fail_papers` và `audit_complete` theo schema thực tế. Không dùng giá trị mặc định 0 để che khóa thiếu.
- Muốn audit toàn nhóm từ một lượt cá nhân, đặt `GROUP_OUTPUT` đến corpus/checkpoints chung đã tổng hợp đủ cấu trúc. Kết quả riêng là `group-assignment-audit.json`. Chưa có dữ liệu chung thì notebook thông báo chưa audit nhóm; không kết luận cả nhóm thiếu dữ liệu chỉ từ thư mục một người. Chế độ `ALL_GROUP_25K` audit trực tiếp toàn assignment trên output của nó.
- Statistics chỉ xuất lại sau khi run ID và hash snapshot khớp trước/sau revalidate; file xuất mang cùng run ID và label. Tỷ lệ `null` hiển thị không áp dụng/chưa có mẫu số, khác với 0%.
- Chỉ chạy một tiến trình ghi trên mỗi output và tái kiểm sau khi các writer đã dừng. Đường dẫn audit đầu vào và các lần audit được lưu trong artifacts. Drive và Colab vẫn có giới hạn I/O, quota và thời lượng; cần kiểm tra storage và chạy smoke/resume trước lượt dài.

## Bảo trì và kiểm thử

Code của bốn cell chính ở `cells/`. Sau khi sửa, chạy `python scripts/sync_colab_notebook.py` từ repo root để nhúng code vào notebook; người dùng Colab chỉ cần file ipynb. `scripts/prepare_notebooks.py` cũng gọi bước sync, tránh tạo lại notebook Colab phiên bản cũ.

Tests chạy trực tiếp source cell trong notebook với subprocess giả lập; audit và kiểm tra receipt/hash dùng module thật từ source repo đã cài. Không gọi API, mount Drive hay crawl trong tests:

```powershell
python -m pip install -e ../arxiv-data-science[reporting,dev]
python -m unittest discover -s tests -v
```

Các ca kiểm tra gồm chạy lại vẫn giữ artifacts/checkpoint, dải cá nhân/nhóm/smoke, báo đúng unseen, null và zero rates, setup thất bại chặn chạy, sai run ID/hash hoặc snapshot thay đổi khi revalidate, audit lỗi không dùng lại báo cáo cũ.

Các kiểm thử này không thay thế một lượt chạy thật trên Google Colab/Drive.
