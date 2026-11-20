# Notebook chạy trên CPU

## Milestone 1: phục hồi chọn lọc

Mở [MS1_Selective_Recovery_CPU.ipynb](MS1_Selective_Recovery_CPU.ipynb) bằng Colab CPU.
Notebook có ba cell chạy: kiểm kê, phục hồi, kiểm định/bàn giao. Chọn TV1–TV5 và
nhập đường dẫn đến thư mục `data/` MS1 đang có. `AUTO` dùng đường dẫn của notebook
MS1 cũ: `MyDrive/arxiv_ms1_data/tvN/data`. Thư mục kết quả mặc định riêng tại
`MyDrive/arxiv_ms1_recovery/tvN`; mỗi TV chỉ dùng output của mình.

Bước 2 mặc định **Chỉ kiểm kê**. Xem các hành động được in và `inventory.csv`
trước khi đổi sang **Phục hồi local** (extract/metadata/QA) hoặc **Phục hồi có
tải/API** (cho phép download/references/discovery cần sửa). Dừng mọi tiến trình
ghi nguồn trước khi chạy; không mở nhiều runtime ghi cùng thư mục kết quả.

Code bản vá local đã nhúng kèm SHA-256 trong notebook, không clone nhánh GitHub.
Chép dữ liệu bằng tối đa 32 luồng về ổ Colab rồi mới hash/giải nén/xử lý. Cần chỗ
cho một bản đầy đủ trên Colab và một bản phục hồi riêng trên Drive. Bản nền trên
Drive chỉ được tạo khi bật phục hồi; sau đó lưu artifact/checkpoint của từng stage.
Khi kết thúc, file đã đồng bộ và còn khớp được bỏ qua khi xuất lại dữ liệu.

Sau khi mất runtime, giữ nguyên thành viên/đường dẫn và chạy lại các bước. Notebook
chép bản phục hồi đã lưu về local và kiểm tra lại tính toàn vẹn; stage tốt không
gọi API lại. Việc chép dữ liệu khi Colab mất ổ local vẫn cần thời gian. Stage bị
ngắt ngay lúc ghi hoặc đồng bộ có thể phải chạy lại.

Bước 3 cập nhật `data/manifests`, QA, summary và snapshot receipt, xuất ZIP bằng
chứng có audit/danh sách sửa/lỗi còn lại. **ZIP không chứa raw/corpus**; bàn giao
cả link `data/` và ZIP. Kiểm tra `data/manifests/recovery.json`:
`handoff_ready=true` mới được dùng làm snapshot MS2 trong phạm vi đã kiểm định.
Nếu chưa từng phục hồi trên output này, bước 3 chỉ xuất báo cáo kiểm kê. Nếu đã
có lượt phục hồi kết thúc được lưu trong `evidence/*/run.json`, notebook nhận lại
lượt đó khi chạy lại bước 1, kể cả sau khi đổi runtime. Có thể chạy bước 1 rồi
bước 3 để xuất bàn giao; không cần tải/extract lại chỉ để lấy báo cáo. ZIP giữ
nhật ký các lượt trước trong `source-runs.json`.

Đã có kết quả phục hồi TV5 trên Drive ngày 19/11/2026; còn 52 paper chưa đạt trong
kiểm kê sau sửa. Bản vá xuất bàn giao sau khi mở lại session được kiểm thử local;
chưa áp dụng bản vá này để ghi lại kết quả Drive. Khi sửa code, tạo lại file phân
phối rồi chạy tests:

```powershell
python scripts/build_ms1_recovery_notebook.py
python -m pytest tests/pipeline/test_ms1_recovery.py tests/pipeline/test_ms1_recovery_colab.py tests/test_ms1_recovery_notebook.py -q
```

## Milestone 2: tích hợp và nghiệm thu

Mở [MS2_Integration_CPU.ipynb](MS2_Integration_CPU.ipynb). Notebook dùng module
thật của TV1–TV5, giữ MS1/MS2 ở hai root riêng, đo thời gian/RAM và thử resume.
Mỗi người chọn `MEMBER` (tv1–tv5), nhập `MS1_INPUT` là data MS1 sau phục hồi và
chạy `RUN_MODE='SMOKE'` trước. Sau đó đổi sang `FULL`, chạy lại cell lựa chọn,
cell xác nhận phạm vi và cell parsing. Không cần sửa `JOBS` hoặc gõ lại range:
notebook đọc đúng phân công 5.000 ID từ `configs/ms1_group6_ranges.json`.
FULL chọn tường minh mọi ID được giao, ghi nhận cả ID thiếu discovery.
Notebook pin `CODE_REF=3ae67d5553eef652d92e5386eef4248b9d124779`, commit đã merge
bản sửa MS1/MS2 qua PR #28. Để trống `MS1_INPUT` dùng
`/content/drive/MyDrive/arxiv_ms1_recovery/<member>/data`. Nếu dữ liệu nằm ở
thư mục hoặc shortcut khác, nhập đường dẫn mount thực tế đến `data/`, không dùng
URL Drive hoặc ZIP bằng chứng. Dữ liệu MS1 cần ổn định trước khi chạy.
Mỗi lô mặc định có 50 ID, dùng 8 luồng chép source/checkpoint cần thiết về
`/content/arxiv_ms2_local`; parser và QA đọc/ghi trên ổ local. Snapshot MS1 cần
được giữ cố định suốt lượt chạy. Source của lô được dọn sau khi lưu checkpoint,
output local tích lũy để tạo ZIP cuối. Cần đủ dung lượng cho source một lô,
output của lượt chạy và ZIP xuất cuối.

Khi chuyển sang bản sửa, dừng cell cũ và đợi dừng hẳn rồi mở notebook mới trong
runtime sạch. Bản này dùng `RUN_TAG='ms2-repaired-20261119'`, ghi output riêng;
giữ kết quả cũ để đối chiếu. Parser đã thay đổi nên không ép completion index
hoặc receipt của runtime cũ thành tương thích. Lượt đầu của runtime mới cần
tính lại các stage liên quan; không cần tải lại MS1 nếu raw/data đã phục hồi tốt.

Chạy mới tự tạo `completion-index.json`. Các lần resume sau giữ nguyên tag mới,
thành viên, input và snapshot: bài đã qua validation được bỏ qua mà không chép
lại source. Notebook ưu tiên ID mới rồi thử lại bài có receipt chưa hoàn tất.
Cell parsing in số ID đã lưu, giây/ID và số stage dùng cache. Index/receipt khác
context làm notebook dừng trước khi sửa output; không đổi về tag cũ để bỏ qua
kiểm tra này. Snapshot thay đổi thì dùng một tag riêng khác.

Bản local đang bổ sung `LEGACY_MS2_INPUT` để dùng lại từng stage của MS2
trước bản vá. Backend đọc checkpoint/dependencies/artifact từ thư mục kết quả
thành viên hoặc `local-batches/`, xác minh hash producer và đầu vào được stage
sử dụng. Chỉ hierarchy/normalize đủ bằng chứng được chuyển tiếp; các producer
đã sửa và validation vẫn chạy theo bản hiện tại. Bài đã hoàn tất trên bản sửa
tiếp tục được bỏ qua, không đọc lại MS2 cũ. Giữ tag mới khi resume.

Để trống cấu hình này sẽ tìm
`<OUTPUT_ROOT>/ms2-members-20261110/full/<MEMBER>` trước; nếu dùng shortcut,
nhập đường dẫn mount tới thư mục thành viên. Output ghi `migration-reports/*.json`
và `migrated_stages`; bằng chứng hash của stage dùng lại nằm trong dependencies
của checkpoint. Thư mục MS2 cũ chỉ đọc.

Phần chuyển tiếp này **chưa phát hành runtime**. `CODE_REF` vẫn là commit đã duyệt
ở trên. Notebook local dừng trước parsing nếu có nguồn MS2 cũ nhưng runtime
chưa hỗ trợ. Cần review, duyệt push/merge backend rồi cập nhật pin trước khi
nhóm dùng phần này trên Colab. Lần đầu vẫn cần chép source MS1 và chạy
resolver/bibliography hiện tại; chưa có benchmark tốc độ trên toàn corpus.

Pilot local trên dữ liệu TV5 sau phục hồi đạt 14/15 paper, không phải nghiệm thu
toàn bộ 5.000 ID. `2410.03415` v1/v2 còn `empty_include` do source chứa `\input{}`;
giữ lỗi để review, không coi là no_source hay hoàn tất. Chạy SMOKE trước, rồi
mới FULL theo dải được giao. Bản này chưa được benchmark đầy đủ trên Colab.

`acceptance.json` ghi bài/version thành công, `no_source`, lỗi, thiếu đầu vào và
cảnh báo. Output tách theo `<OUTPUT_ROOT>/<RUN_TAG>/<smoke|full>/<MEMBER>/`;
các thành viên chạy song song trên output riêng. Không chạy hai runtime ghi
cùng output của một người. Notebook cá nhân không ghi group receipt dùng chung.
Sau mỗi lô, output/checkpoint được đóng thành một ZIP trong `local-batches/`,
kèm receipt và checksum. Receipt chỉ ghi khi ZIP đã copy xong. Runtime bị ngắt
sẽ khôi phục các gói này và thử lại phần chưa lưu. Giữ `local-batches/` khi
tiếp tục. Lô dùng lại hoàn toàn checkpoint đã đóng gói sẽ giữ gói hiện có.

Khi hoàn tất, notebook tự xuất `MS2_RESULTS.zip`, `acceptance.json` và
`delivery.json` trong output cá nhân trên Drive. ZIP chứa thư mục thành viên
với đầy đủ tables, parsed, state, validation, manifests và summaries. Dùng ZIP
này để bàn giao kết quả mới; các thư mục rời từ lượt cũ mang dữ liệu lượt cũ.
`delivery.json` có trạng thái published và SHA-256 của ZIP đã xuất.

Thời gian benchmark gồm đọc discovery, chép input, khôi phục checkpoint,
parsing/QA local, lưu gói theo lô và thu thập output cũ. RAM đo trong bước
discovery và xử lý bài còn lại. Thời gian đóng gói/upload ZIP cuối ghi riêng
trong delivery. Số stage dùng cache được ghi trong acceptance. SMOKE kiểm tra
resume thêm trên local; FULL có thể bật `VERIFY_RESUME` nếu cần.
Notebook này chưa được thực thi trên Colab trong lần nghiệm thu local.

## Milestone 1

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
