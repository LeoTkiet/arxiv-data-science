# TV1 Resolver — triển khai phần B

Phạm vi được người dùng yêu cầu: B1–B13 trong checklist MS2; giữ adapter và
gathered schema của PR9, không sửa Hierarchy PR10, không làm Orchestrator phần C.

## Thiết kế triển khai

- `parsing/resolver.py`: bộ đọc TeX theo cú pháp tĩnh, không chạy LaTeX/shell;
  chọn root ổn định, DFS include với stack chống vòng lặp, giữ include lặp.
- Giữ các mảnh văn bản gắn path/offset nguồn, để source_spans sau ghép/cắt vẫn đúng.
- Bảo vệ verbatim/verb và macro definitions khỏi việc nhận nhầm lệnh include.
- Include tìm từ thư mục file hiện tại rồi corpus root; không vượt corpus version.
- Giữ preamble, chỉ đưa body vào merged_body_tex; kết thúc khi gặp end document.
- Tách thebibliography và mục References chứa bibitem, giữ các heading tiếp theo.
- Chỉ nhận input MS1 có checkpoint và bằng chứng nhất quán; lỗi không thành no_source.
- Ghi gathered atomic, trả StageResult resolve schema 2.0; không sửa input.
- TeX động không thể suy ra đường dẫn bằng cú pháp tĩnh sẽ trả data_error tường minh.

## Các bước và kiểm chứng

- [x] Viết `tests/parsing/test_resolver.py` với corpus/checkpoint giả lập trong thư mục tạm; chạy RED.
- [x] Cài đặt scanner/DFS/reference extraction và adapter; chạy tests Resolver GREEN.
- [x] Rà soát lỗi đường dẫn, input trạng thái và giới hạn cú pháp; bổ sung regression tests nếu cần.
- [x] Chạy toàn bộ tests MS1/MS2 và diff check, cập nhật tài liệu/checklist đúng tiến độ.

## Bằng chứng thực hiện

- RED: Resolver ban đầu NotImplementedError, các tests hành vi thất bại như dự kiến.
- GREEN: 35 tests trước review; review độc lập phát hiện thêm năm trường hợp
  include trong title, empty checkpoint mâu thuẫn inventory, literal header có
  comment, environment gọi macro include gián tiếp, và appendix command sau refs.
- Mỗi finding có test RED rồi sửa GREEN: 40 tests Resolver passed, 1 skipped
  (quyền tạo symlink trên host Windows), 16 subtests passed, 0.86s.
- Suite cuối: 191 passed, 1 skipped, 61 subtests passed trong 19.29s;
  thư mục tạm pytest riêng dưới artifacts, không gọi mạng/corpus thật.
- Không đổi hành vi MS1, không sửa Hierarchy PR10, chưa làm Orchestrator C.
- Quyết định về giới hạn cú pháp được công bố trong MS2_RESOLVER.md:
  không chạy TeX engine/macro động; hỗ trợ nguồn UTF8 và Latin1 có cảnh báo;
  giới hạn DFS/source/expanded text để không trả output một phần.

Không commit/push trong bước triển khai này nếu chưa có yêu cầu tiếp theo.
