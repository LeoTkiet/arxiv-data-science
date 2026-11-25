# T3-05: chẩn đoán pool Auto sau lượt thử đầu

Pool chuẩn bị ban đầu có 1.659 bài `ready`, trong đó 68 bài không có BibTeX
entry. Vì vậy ngay cả khi mọi bài còn lại đạt, chỉ có tối đa 1.591 bài có
nhãn, thấp hơn quota 1.630. Trong 100 bài heuristic đầu, chỉ 9 bài phủ đủ;
rule `abstain_ambiguous` chiếm phần lớn các dòng `uncertain`. Không chuyển
`uncertain` thành `unmatched` để lấp quota.

Mở `colab/MS3_T3_05_Auto_Pool_Diagnostic_CPU.ipynb`:

1. Mount Drive, cập nhật nhánh T3-05.
2. Dùng cache ZIP MS2 nếu runtime còn giữ; nếu không, tải lại đúng năm file
   bằng Drive ID, kiểm tra size/MD5, không giải nén.
3. Quét BibTeX của các bài MS2 complete chưa chọn. Lấy **pilot 50 bài mỗi TV**
   có title/authors đầy đủ và số BibTeX entry ít hơn. Đây là mẫu thử chất lượng
   metadata, không phải mẫu ngẫu nhiên hay bộ nhãn được duyệt. Chuẩn bị bundle
   và kiểm tra MS1/MS2 như pool đầu.
4. Gán nhãn heuristic trên pilot để đo bài phủ đủ và số abstain. Dựa vào kết
   quả này mới quyết định mở rộng pool, cải thiện rule có chứng cứ hoặc cần
   review thủ công.

Để đo trên phạm vi rộng hơn, cell 3 hiện lấy mẫu phân tầng tối đa **800 bài
mỗi TV** theo độ đầy đủ title/authors và số BibTeX. Không chọn riêng các hàng
metadata tốt nhất; mục tiêu là pilot ít thiên lệch hơn, không phải cam kết đạt
quota. Nếu chỉ một số strata đạt, xem `candidate_scan.json` trước khi chuẩn bị
mọi bundle.

Cell 4 xuất tới 100 dòng spot-check xác định theo seed/hash, tối đa 20 mỗi tổ
`member × rule`. Đây là **mẫu cần người xác minh**, không phải nhãn chuẩn. Chỉ
tính precision sau khi điền kết quả kiểm tra độc lập; không tính recall từ mẫu
accepted đơn thuần vì chưa có nhãn gold cho các unmatched và uncertain.

Kết quả nằm ở `T3-05/output/diagnostic/`: `candidate_scan.json`,
`pilot_selection.json`, `prepared/prepared_manifest.json`,
`first_pass/first_pass_manifest.json` và nhãn nháp. Pilot **không được bàn giao
cho TV5** khi chưa qua spot-check, validate và chốt coverage.

## Kết quả pilot ban đầu và cách hiểu

Pilot đầu có 219 bài ready và 86 bài phủ đủ, nhưng 63/86 bài phủ đủ không có
reference ứng viên. Trong 156 bài có references, chỉ 23 bài phủ đủ (14,7%).
Vì vậy không được dùng tỷ lệ 86/219 để dự đoán chất lượng matching hoặc mở rộng
pool theo cùng tiêu chí mà không kiểm tra thêm. Cần xem riêng coverage của bài
có references, tỷ lệ matched/unmatched và spot-check các rule trước khi mở rộng.
