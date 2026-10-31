# Checklist Milestone 2 — TV1

MS1 đang thu thập/làm sạch dữ liệu, dự kiến còn 10–12 tiếng theo ước lượng của nhóm. TV1 có thể triển khai A–D bằng dữ liệu giả lập ngay; kiểm chứng dữ liệu thật và nghiệm thu ở E khi đầu vào đã hoàn tất. Các mục chưa đánh dấu chưa được xác nhận hoàn thành.

## A. Chuẩn bị kiến trúc và hợp đồng dữ liệu

- [x] A1 Kiểm tra đầu ra Milestone 1 — Hiểu cấu trúc `corpus/<paper_key>/vN/`, `DiscoveryRecord`, `VersionRecord` và `StageResult`.
- [x] A2 Xây dựng 3 schema đầu ra — Bổ sung `ElementRow`, `HierarchyRow`, `BibtexRow` vào `common/schemas.py`.
- [x] A3 Chuẩn hóa interface giữa các module — Thống nhất `run(record, output_dir, *, version=None, upstream=None) -> StageResult`.
- [x] A4 Chốt định dạng artifact — Thống nhất trường dữ liệu của `gathered.json`, `raw_tree.json`, `normalized_tree.json`, `raw_bibtex.json` và các bảng đầu ra.
- [ ] A5 Cập nhật hợp đồng dữ liệu — Bổ sung đặc tả Milestone 2 vào `docs/CONTRACTS.md` và gửi TV2–TV5 xác nhận.

- [x] A6 Chốt điều kiện bàn giao MS1 → MS2 — Chỉ parse version khi extract đã thành công và corpus đã ghi xong. `VersionRecord` hiện không có trường status; quy định checkpoint/kết quả download và extract nào cung cấp trạng thái. Phân biệt `no_source` được xác nhận với chưa sẵn sàng, lỗi tải và lỗi giải nén; không kết luận PDF-only từ thư mục thiếu/rỗng.
- [x] A7 Cụ thể hóa hợp đồng A2–A5 — Cung cấp ví dụ JSON đầu vào/đầu ra từng stage, gồm tên stage/artifact, trường bắt buộc, artifact rỗng, trạng thái lỗi và đường dẫn tương đối. Chốt cách biểu diễn root, thứ tự nút/cạnh, phần tử cùng nội dung xuất hiện nhiều lần và đầu vào nhiều version của deduplication. Giữ tương thích MS1; quy định phiên bản schema khi thay đổi hợp đồng.
- [x] A8 Chuẩn bị dữ liệu phát triển — Dùng chuỗi TeX/BibTeX giả lập trong tests và cây file trong thư mục tạm. Khi có mẫu thật, dùng bản sao MS1 đã hoàn tất; tách output MS2 khỏi output MS1 đang chạy, quy định rõ input root/output root và không sửa corpus đầu vào.

### Kết quả phần A — 29/10/2026

- Đã chuẩn bị baseline nội bộ cho A1–A4, A6–A8; dấu hoàn thành ở đây chỉ xác nhận triển khai local, chưa thay cho đồng thuận TV2–TV5 hoặc nghiệm thu corpus thật.
- Schema: `arxiv-data-science/src/arxiv_pipeline/common/schemas.py`; `hierarchy` giữ đúng 4 cột theo đề. MS1 giữ schema mặc định 1.0; artifact/StageResult MS2 dùng phiên bản riêng 2.0.
- Hợp đồng và audit: [MS2_CONTRACTS.md](../MS2_CONTRACTS.md). Bản bàn giao: [MS2_HANDOFF.md](../MS2_HANDOFF.md).
- Ví dụ và corpus giả lập: `arxiv-data-science/tests/parsing/ms2_contract_examples.py`; có 8 test hợp đồng, cây occurrences giữ thứ tự/lần xuất hiện; fixtures chỉ ghi vào thư mục tạm.
- Kiểm chứng toàn repo: **148 tests và 45 subtests qua**. Lượt đầu vướng quyền thư mục tạm pytest; chạy lại với `--basetemp artifacts/pytest-ms2-<unique-id>` đã qua. `git diff --check` không báo lỗi.
- **A5 còn chờ:** tài liệu đã cập nhật, chưa gửi và chưa nhận xác nhận từ TV2–TV5. Chưa có snapshot MS1 thực tế được bàn giao; khi nhận mẫu, vẫn phải kiểm tra theo A6/E2.
- Chưa triển khai B/C. Theo yêu cầu push tiếp theo, phần A được tách sang nhánh `feature/m2-tv1-contracts` từ `origin/main`, không gộp vào PR MS1.

## B. Xây dựng Resolver — Module chính của TV1

- [ ] B1 Xử lý LaTeX comments — Bỏ các lệnh bị comment bởi `%`, nhưng xử lý đúng dấu `\%` đã escape.
- [ ] B2 Tìm file TeX chính — Xác định file gốc chứa `\documentclass`, `\begin{document}` và `\end{document}`.
- [ ] B3 Ghép file theo đường dẫn biên dịch — Duyệt `\input`, `\include` đệ quy nhiều cấp, bảo toàn thứ tự nội dung.
- [ ] B4 Loại bỏ các file TeX không liên quan — Ghi nhận danh sách `ignored_files` và tránh ghép file bản nháp.
- [ ] B5 Tách tài liệu tham khảo — Lấy `raw_bibitems`, thu thập file `.bib`, loại khối `thebibliography` khỏi nội dung chính.
- [ ] B6 Xử lý phiên bản không có TeX — Trả `no_source` an toàn, không làm dừng toàn bộ pipeline.
- [ ] B7 Tạo artifact gathered.json — Ghi đầu ra và trả `StageResult` theo hợp đồng.
- [ ] B8 Viết unit tests cho Resolver — Kiểm tra file đơn, đa file, comment, references, vòng lặp include, PDF-only.

- [ ] B9 Hoàn thiện quy tắc include — Chốt cách tìm file từ thư mục con, xử lý file đích thiếu và chặn đường dẫn thoát khỏi corpus của version. Dùng stack của nhánh đang duyệt để chặn vòng lặp, vẫn giữ các lần include lặp hợp lệ và thứ tự nội dung.
- [ ] B10 Hoàn thiện chọn root và truy vết — Khi có nhiều ứng viên, chọn theo quy tắc ổn định và lưu lý do/cảnh báo; quy định kết quả khi không tìm được root hoặc ranh giới document nằm trong file include. Lưu thông tin file nguồn phục vụ truy vết; `ignored_files` chỉ biểu thị file không được sử dụng, không xóa file đầu vào.
- [ ] B11 Giữ ngữ cảnh và tách References đúng phạm vi — Bàn giao ngữ cảnh preamble cần thiết riêng với body; giữ Appendix/Acknowledgements sau References. Nhận diện lệnh khai báo bibliography, phân biệt `.bib` được tham chiếu với `.bib` chỉ có trong thư mục và cảnh báo nguồn references bị thiếu.
- [ ] B12 Ghi artifact an toàn — Ghi atomic rồi mới trả `success`; bổ sung thông tin schema, paper/version, provenance và cảnh báo theo hợp đồng. Ghi rõ cú pháp LaTeX được hỗ trợ và cách báo phần chưa hỗ trợ; đầu vào hỏng/chưa hoàn tất không được đổi thành `no_source`.
- [ ] B13 Mở rộng tests B8 — Thêm thư mục con, bảo toàn thứ tự, include lặp, file đích thiếu, đường dẫn ngoài corpus, root mơ hồ, escaped percent, Appendix sau References và trạng thái đầu vào lỗi/chưa sẵn sàng. Tests không gọi mạng hoặc phụ thuộc corpus thật.

## C. Framework và Pipeline Orchestrator

- [ ] C1 Thiết kế luồng điều phối — Theo version: `Resolver → Hierarchy → Normalization` và `Resolver → Bibliography`. Theo paper: gom Normalization/Bibliography của các version rồi `Deduplication → Validation MS2`; xử lý riêng version `no_source` theo hợp đồng.
- [ ] C2 Triển khai pipeline/parse.py — Kết nối Resolver, Hierarchy, Normalization, Bibliography, Deduplication và Validation.
- [ ] C3 Mở rộng CLI — Cho phép chạy parsing bằng tham số CLI, đồng thời giữ nguyên chức năng Milestone 1.
- [ ] C4 Hỗ trợ trạng thái và checkpoint — Xử lý `success`, `no_source`, `data_error`, `error`, cùng cơ chế `--resume` và `--force` phù hợp.
- [ ] C5 Kiểm thử tích hợp bằng mock adapters — Mô phỏng TV2–TV5 và xác minh thứ tự gọi, truyền artifact, xử lý lỗi.

- [ ] C6 Chốt tính đầy đủ cấp paper — Đối chiếu danh sách version kỳ vọng với các version đã xử lý và outcome `no_source`. Nếu còn version thiếu/lỗi/chưa sẵn sàng, ghi rõ kết quả chưa đầy đủ; không công bố bảng deduplication hoặc validation một phần như kết quả cuối cùng.
- [ ] C7 Bổ sung quy tắc resume/force — Kiểm tra artifact và dấu nhận diện đầu vào/phụ thuộc phù hợp. Khi version mới xuất hiện hoặc đầu vào thay đổi, chạy lại các stage bị ảnh hưởng, gồm deduplication/validation cấp paper. `--force` chỉ làm mới kết quả MS2 trong phạm vi được chọn; không ghi đè dữ liệu MS1.
- [ ] C8 Tích hợp validation với TV5 — `export/validation.py` hiện phục vụ MS1; chốt cách gọi và phân biệt checkpoint/artifact validation MS2, giữ hành vi crawl MS1. Kiểm tra stage/paper/version và artifact nằm trong output root; truyền artifact tường minh, gồm nhánh Resolver → Bibliography.
- [ ] C9 Mở rộng tests C5 — Kiểm tra nhánh Bibliography, gom nhiều version, downstream bị chặn, `no_source`, artifact thiếu, resume/force và chạy lại kết quả cấp paper khi có version mới. Xác minh CLI MS1 vẫn hoạt động.

## D. Báo cáo Milestone 2

- [ ] D1 Tạo milestone2/index.md — Khởi tạo chương Milestone 2 trong Jupyter Book.
- [ ] D2 Tạo milestone2/introduction.md — Giới thiệu mục tiêu, dữ liệu đầu vào, đầu ra và tổng quan pipeline.
- [ ] D3 Cập nhật _toc.yml — Đưa chương mới vào mục lục của Jupyter Book đang sử dụng.
- [ ] D4 Viết nội dung phương pháp của TV1 — Bàn giao thuyết minh Resolver và Orchestrator cho `milestone2/methodology.md`.

- [ ] D5 Hoàn thiện nội dung và phối hợp báo cáo — Ghi điều kiện bàn giao MS1, trạng thái dữ liệu, giới hạn cú pháp, xử lý lỗi và resume xuyên version. Phối hợp TV5 khi sửa `_toc.yml`/tích hợp `methodology.md` dùng chung và kiểm tra Jupyter Book build.

## E. Tích hợp và nghiệm thu

- [ ] E1 Tích hợp module thật của TV2–TV5 — Thay mock adapters bằng module đã được merge và giải quyết lỗi hợp đồng.
- [ ] E2 Chạy thử trên corpus mẫu thực tế — Dùng bản sao mẫu MS1 đã hoàn tất: bài một file, đa file, nhiều version và `no_source`; thêm trường hợp bất thường nếu có. Ghi nguồn mẫu, version và giới hạn về độ đại diện.
- [ ] E3 Chạy kiểm thử toàn bộ pipeline — Đảm bảo xuất được các bảng `elements`, `hierarchy`, `bibtex` và kết quả validation.
- [ ] E4 Hỗ trợ nghiệm thu toàn corpus — Phối hợp TV5 xử lý lỗi tích hợp khi chạy Colab CPU-only và tổng hợp báo cáo.

- [ ] E5 Kiểm tra nghiệm thu mở rộng — Kiểm tra khóa/quan hệ và thứ tự cấu trúc, đồng bộ citation, deduplication xuyên version và resume không giữ kết quả cấp paper lỗi thời. Sau bàn giao MS1, thống kê riêng thành công, `no_source`, lỗi, chưa xử lý và paper chưa đủ version; không dùng chạy mẫu thay nghiệm thu toàn corpus.

## Thứ tự triển khai trong lúc chờ MS1

1. **Làm ngay:** A1–A8; ưu tiên bàn giao schema, ví dụ artifact và hợp đồng cho TV2–TV5.
2. **Khi MS1 vẫn chạy:** B1–B13, C1–C9 bằng dữ liệu giả lập/mock adapters; chuẩn bị D1–D5.
3. **Khi có mẫu MS1 hoàn tất:** Chạy E2 trên bản sao mẫu, sửa lỗi; thực hiện E1 khi module thật sẵn sàng.
4. **Khi đủ đầu vào và module:** Hoàn tất E1–E5, kiểm tra tính đầy đủ theo paper/version và nghiệm thu toàn corpus.
