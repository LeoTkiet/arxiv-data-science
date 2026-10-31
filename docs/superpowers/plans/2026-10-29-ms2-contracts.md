# MS2 Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hoàn thiện phần A của checklist TV1 để các module MS2 dùng chung hợp đồng.

**Architecture:** Bổ sung dataclass và hợp đồng MS2 riêng; giữ MS1 1.0. Ba bảng theo đề, occurrence sidecar giữ thứ tự/các lần xuất hiện.

**Tech Stack:** Python >=3.10, dataclasses, unittest/pytest; không thêm dependency.

**Spec:** `docs/superpowers/specs/2026-10-29-ms2-contract-design.md`

## Global Constraints

- `hierarchy` đúng bốn cột `arxiv_id`, `version`, `element_id`, `parent_id`.
- Không sửa corpus MS1; fixture chỉ ở Python, ghi file vào thư mục tạm.
- Không thay đổi chữ ký adapter hoặc schema mặc định MS1.
- Chỉ triển khai phần A, không làm Resolver/Orchestrator.

## Review Focus

- Câu lặp cùng cha phải còn hai occurrences với position khác nhau.
- Version empty không tạo document element giả.
- Bảng chính phải tái hiện ví dụ 5 elements/4 hierarchy rows của đề.
- Path MS1 phải được phân giải từ input root, artifact MS2 từ output root.
- StageResult MS1 vẫn serialize schema_version 1.0.

## Task 1: Schema, examples và fixture

Files: `common/schemas.py`, `tests/parsing/ms2_contract_examples.py`, `tests/parsing/test_ms2_contracts.py`.
Interfaces: `ElementRow`, `HierarchyRow`, `BibtexRow`; `examples()` và `write_corpus_fixture(input_root)` trong test helper.

- [x] Viết tests cho các ràng buộc Review Focus và chạy để xác nhận thiếu schema/helper.
- [x] Bổ sung dataclass và ví dụ executable tối thiểu.
- [x] Chạy tests mới, sau đó chạy regression MS1.

## Task 2: Hợp đồng và bàn giao

Files: `docs/CONTRACTS.md`, `docs/MS2_CONTRACTS.md`, `docs/MS2_HANDOFF.md`, checklist ngoài repo.
Interfaces: Adapter giữ chữ ký cũ, envelope/JSON theo `MS2_CONTRACTS.md`.

- [x] Ghi audit A1, điều kiện ready/empty/error, envelope theo stage, JSON fields và output paths.
- [x] Đối chiếu fixtures với hợp đồng, ghi bằng chứng tests và trạng thái chờ xác nhận TV2–TV5.
- [x] Cập nhật checklist theo công việc thực tế; không đánh hoàn tất việc gửi/xác nhận chưa diễn ra.

## Execution ledger

- Schema/example tests RED (thiếu BibtexRow), rồi GREEN: 6 tests.
- Review độc lập phát hiện mâu thuẫn bib-only. Thêm tests RED cho bảo toàn
  references và cảnh báo provenance/empty, sửa ví dụ và hợp đồng, GREEN: 8 tests.
- Toàn suite lần đầu: 138 passed, 8 lỗi quyền thư mục temp mặc định.
  Chuyển basetemp sang đường dẫn mới trong artifacts; lần cuối 148 passed,
  45 subtests passed (16.39s). Diff check sạch.
- Gửi/xác nhận nhóm và dữ liệu thật chưa xảy ra, giữ A5 pending.

Ban đầu chỉ chuẩn bị thay đổi local; yêu cầu tiếp theo của người dùng cho phép
commit/push phần A trên nhánh riêng `feature/m2-tv1-contracts`.
