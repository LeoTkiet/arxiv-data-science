# Thiết kế hợp đồng MS2 — phần A

## Quyết định đã chốt

Người dùng yêu cầu triển khai A của `checklist_MS2.md` và chốt giữ `hierarchy`
đúng bốn cột `arxiv_id`, `version`, `element_id`, `parent_id` theo đề chính thức.
Lưu thứ tự và lần xuất hiện trong artifact phụ; không thêm cột vào bảng chính.

## Phương án và phạm vi

Giữ ba bảng đúng đề, bổ sung occurrence artifact là phương án được chọn.
Mở rộng bảng hierarchy giúp truy vấn thuận tiện nhưng khác schema đề, nên loại bỏ.
Chỉ bổ sung schema, hợp đồng, ví dụ bằng Python và tests; Resolver/Orchestrator
thuộc B/C chưa triển khai. Không đổi hành vi crawl hoặc validator MS1.

## Quyết định triển khai

- Dataclass đầu ra bổ sung trong `common/schemas.py`; MS1 vẫn dùng schema 1.0.
- Artifact và StageResult MS2 dùng phiên bản hợp đồng riêng 2.0.
- Adapter giữ chữ ký hiện có. `upstream` là envelope tường minh gồm context và stages.
- Input root MS1 chỉ truyền lúc chạy; mọi đường dẫn trong artifact ghi đĩa đều tương đối.
- Các cây trung gian giữ temp ID/parent và position. Artifact occurrences lưu
  từng lần xuất hiện với ID riêng, liên kết về element đã dedup.
- Không có cạnh root → null trong hierarchy; root có parent null chỉ trong cây phụ.
- Nhận diện empty theo bằng chứng checkpoint, không theo thư mục rỗng.
- Dữ liệu giả lập nằm trong source Python; tests dựng corpus trong thư mục tạm.
- Bàn giao cho nhóm ở trạng thái chờ xác nhận, không giả định thành viên đã đồng ý.

## Kiểm chứng

Tests kiểm tra schema đúng đề, JSON serializable, ví dụ hai version của đề,
occurrence lặp và fixture độc lập input/output. Chạy bộ kiểm thử MS1 để phát hiện hồi quy.

Nguồn: https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/parsing/
