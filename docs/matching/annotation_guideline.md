# Annotation Guideline — Reference Matching (Milestone 3 · TV3)

Tài liệu quy chuẩn quy trình gán nhãn **Gold (Manual)** và **Silver (Heuristic)** cho bài toán **Reference Matching**, tuân thủ [MS3_CONTRACTS.md](../MS3_CONTRACTS.md) và `src/arxiv_pipeline/matching/contracts.py`.

---

## 1. Nguyên tắc bất biến

1. **Matching nội bộ trong cùng publication (Within-publication only):**
   - Mỗi query `(arxiv_id, citation_key)` từ bảng `bibtex` chỉ được đối chiếu với tập `references` (`ref_arxiv_id`) của **chính publication đó**.
   - Tuyệt đối không gán `groundtruth_arxiv_id` từ publication khác hoặc tự điền arXiv ID bên ngoài danh sách `references` của publication.
2. **Tối đa 1 positive reference / query:**
   - Mỗi `(arxiv_id, citation_key)` chỉ có đúng **1** `LabelRecord`.
   - Không được tạo nhãn trùng lặp `(arxiv_id, citation_key)` hoặc xung đột quyết định.
3. **Canonical ID:**
   - Mọi `arxiv_id` và `groundtruth_arxiv_id` phải ở dạng canonical (bỏ hậu tố phiên bản `v1`, `v2`..., ví dụ `2001.00001v1` $\rightarrow$ `2001.00001`), sử dụng `canonical_id()` trong `contracts.py`.

---

## 2. Định nghĩa 3 Quyết định gán nhãn (`decision`)

| `decision` | `groundtruth_arxiv_id` | Ý nghĩa & Điều kiện áp dụng | Đưa vào Supervised Train/Eval? |
| :--- | :--- | :--- | :---: |
| **`matched`** | `ref_arxiv_id` (khác `null`) | Khớp chắc chắn với đúng 1 reference candidate trong publication. | Có (khi `review_status="accepted"`) |
| **`unmatched`** | `null` | Đã kiểm tra toàn bộ candidates và kết luận chắc chắn **không** có reference nào khớp (ví dụ bài báo không có trên arXiv, sách, website, hoặc tập references rỗng). | Có (khi `review_status="accepted"`) |
| **`uncertain`** | `null` | Chưa đủ cơ sở kết luận (thiếu title/author cả hai bên, có $\ge 2$ candidates gần như trùng nhau không phân biệt được, hoặc metadata bị lỗi nặng). **Cấm gán bừa thành `matched` hoặc `unmatched`.** | **Không** (chỉ dùng để review chéo) |

---

## 3. Quy tắc đối chiếu (Matching Rules cho Annotator)

Khi đối chiếu một dòng BibTeX `(title, authors, year, doi, url, journal)` với danh sách `ReferenceInput (ref_arxiv_id, ref_title, ref_authors, ref_submission_date)`:

1. **Khớp qua arXiv ID tường minh:**
   - Nếu BibTeX chứa arXiv ID trong `url` (`arxiv.org/abs/2001.00001`), `doi` (`10.48550/arXiv.2001.00001`), hoặc `journal` (`arXiv preprint arXiv:2001.00001`), đối chiếu trực tiếp với `ref_arxiv_id`.
   - Vẫn kiểm tra nhanh `title` / `authors` để phát hiện lỗi metadata (nếu hoàn toàn mâu thuẫn $\rightarrow$ đánh dấu `uncertain` và ghi `notes`).
2. **Khớp qua Title + Authors + Year:**
   - Bỏ qua khác biệt hoa/thường, dấu câu, lệnh LaTeX (`\textbf`, `$...$`, dấu ngoặc `{}`), ký tự Unicode/accents.
   - **Lưu ý năm (`year` vs `ref_submission_date`):** Năm nộp arXiv (`ref_submission_date`) có thể lệch $\pm 1\text{–}2$ năm so với năm xuất bản hội nghị/tạp chí (`bib.year`). Độ lệch nhỏ này **không** phải lý do loại bỏ match nếu title và authors khớp rõ ràng.
   - Nếu title giống nhưng danh sách tác giả hoàn toàn khác nhau $\rightarrow$ **không** gán `matched` (kiểm tra kỹ xem là nhầm bài hay `unmatched` / `uncertain`).
3. **Nhiều candidates gần giống:**
   - Nếu có $\ge 2$ candidates cùng title/tác giả (ví dụ phần 1 / phần 2 hoặc trùng lặp) và không phân biệt được $\rightarrow$ chọn `uncertain` và ghi rõ các candidate nghi vấn vào `notes`.
4. **Tập references rỗng (`len(references) == 0`):**
   - Không có candidate nào để khớp $\rightarrow$ gán `unmatched`.

---

## 4. Schema Provenance & Trạng thái Review

Mỗi `LabelRecord` tuân thủ `contracts.LabelRecord`:
- `arxiv_id`: canonical ID của bài báo trích dẫn.
- `citation_key`: khóa trích dẫn BibTeX.
- `groundtruth_arxiv_id`: canonical `ref_arxiv_id` hoặc `None`.
- `decision`: `"matched"` | `"unmatched"` | `"uncertain"`.
- `label_source`: `"manual"` | `"heuristic"`.
- `confidence`: `None` (hoặc `[0, 1]`) đối với `manual`; **bắt buộc** trong `[0.0, 1.0]` đối với `heuristic`.
- `annotator`: mã định danh thành viên (ví dụ `"tv1"`, `"tv2"`, `"tv3"`, `"tv4"`, `"tv5"`).
- `reviewer`: mã định danh người review chéo (ví dụ `"tv5"`).
- `review_status`: `"pending"` | `"accepted"` | `"rejected"`.
- `heuristic_version`: bắt buộc (ví dụ `"v1.0.0"`) khi `label_source == "heuristic"`.
- `notes`: ghi chú lý do (bắt buộc khi `decision == "uncertain"` hoặc `review_status == "rejected"`, hoặc tên rule khi dùng heuristic).

> **Chú ý:** Chỉ những nhãn có `review_status == "accepted"` và `decision != "uncertain"` (`LabelRecord.is_supervised == True`) mới được đưa vào tập train/valid/test và đánh giá MRR@5.

---

## 5. Quy định Định lượng (Quota) & QA toàn nhóm (5 người)

### 5.1. Manual Pool (Gold)
- **Toàn nhóm:** Tối thiểu **25 publications**, mỗi publication có **$\ge 20$ BibTeX entries** đã trích xuất $\rightarrow$ tổng **$\ge 500$ entries**.
- **Cá nhân (TV1–TV5):** Mỗi thành viên gán tối thiểu **5 publications** ($\ge 100$ entries/người).
- **Phân tầng chọn bài:** Chọn đa dạng các bài có số references ít/nhiều và độ sạch metadata khác nhau, tránh chỉ chọn bài dễ.
- **QA:** Toàn bộ nhãn `uncertain` và ít nhất một mẫu các nhãn `matched`/`unmatched` phải được reviewer khác (`reviewer != annotator`) kiểm tra chéo trước khi chuyển sang `review_status="accepted"`.

### 5.2. Automatic Pool (Silver)
- **Phạm vi mẫu số:** `non_manual_publications = all_publications_in_scope - manual_publications` (tính trên toàn bộ corpus đầu vào hợp lệ, không thu hẹp mẫu số).
- **Số lượng tối thiểu:** $\max(\lceil 10\% \times |\text{non\_manual\_publications}| \rceil, 10)$ publications.
- **Không áp dụng** ngưỡng $\ge 20$ BibTeX/bài cho auto pool, nhưng **100% BibTeX entries** của các publication được chọn phải có nhãn hoàn chỉnh (các trường hợp `uncertain` / abstain phải được xử lý/review trước khi công bố coverage).
- **Spot-check Audit:** Bắt buộc lấy mẫu ngẫu nhiên từ tập auto-label để gán nhãn tay đối chiếu, báo cáo precision/noise rate thực tế; tuyệt đối không coi auto labels là ground-truth hoàn hảo 100%.

