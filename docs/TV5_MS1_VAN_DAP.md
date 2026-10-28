# TV5 Milestone 1 — Nội dung công việc và ôn vấn đáp

Tài liệu học dựa trên code được triển khai ngày 28/10/2026. Không học thuộc các con số demo như kết quả thực nghiệm của nhóm. Trạng thái nghiệm thu cụ thể xem cuối file và checklist MS1.

## 1. Giới thiệu phần việc trong khoảng một phút

“Em phụ trách TV5: kiểm tra chất lượng corpus, đo hiệu năng và tổng hợp báo cáo. Em dùng đầu ra của discovery, downloader, extractor và metadata/references để kiểm tra đủ version, schema, file và trạng thái. Em viết wrapper chạy pipeline ở tiến trình riêng, đo wall time, lấy mẫu RAM của cả cây tiến trình và dung lượng thư mục output, đồng thời ghi thời gian từng paper và từng bước. Sau đó em tính thống kê với tử số/mẫu số rõ ràng, xuất JSON và dùng notebook sinh bảng, biểu đồ khi build Jupyter Book. Em tách dữ liệu demo với benchmark thực tế và chuẩn bị notebook tái lập trên Colab CPU-only.”

## 2. Các file chính và trách nhiệm

| File trong repository source | Trách nhiệm |
|---|---|
| `src/arxiv_pipeline/export/validation.py` | Kiểm tra từng paper, ghi `validation/<paper_key>.json` |
| `src/arxiv_pipeline/reporting/milestone1/benchmark.py` | Chạy subprocess, lấy mẫu tài nguyên và lưu môi trường/số đo |
| `src/arxiv_pipeline/reporting/milestone1/statistics.py` | Tổng hợp checkpoint/sidecar, tính số lượng và tỷ lệ |
| `src/arxiv_pipeline/reporting/milestone1/export.py` | Xuất bằng chứng gọn sang repository book, loại local paths khỏi JSON công khai |
| `src/arxiv_pipeline/pipeline/crawl.py` | Observer tùy chọn ghi thời gian discovery, stage, paper |
| `src/arxiv_pipeline/main.py` | Thêm cờ `--timings` để ghi sự kiện JSONL |
| `configs/ms1_group6_ranges.json` | Năm khoảng ID của nhóm 6 từ Excel, tổng 25.000 lookup |
| `scripts/demo_ms1.py` | Demo transport offline; không dùng làm benchmark chính thức |
| `tests/test_ms1_reporting.py` | Kiểm thử công thức, tiến trình thật, lỗi, export và resume timing |
| `docs/TV5_MS1_GUIDE.md` | Cách cài, chạy, tái kiểm và diễn giải chỉ số |

Repository report ở thư mục bên cạnh `arxiv-project-report/`: `_config.yml`, `_toc.yml`, chương `milestone1/`, notebook Colab và workflow deploy. Không sửa parser hay module MS2.

## 3. Dữ liệu đi qua hệ thống như thế nào?

1. Discovery nhận ID, tìm mọi version và ghi metadata sơ bộ.
2. Downloader tải từng source version và sidecar checksum/kích thước/trạng thái.
3. Extractor tạo corpus `.tex`/`.bib` tách riêng mỗi version; sidecar ghi kích thước trước/sau lọc.
4. Metadata chuẩn hóa dữ liệu discovery; references lấy thông tin từ Semantic Scholar và lưu provenance.
5. Validator đọc checkpoint từng stage để kiểm tra đầu ra của paper.
6. Pipeline hoàn tất mới ghi manifest và summary tổng hợp.
7. TV5 đọc snapshot hoàn tất, xuất thống kê và benchmark sang book; notebook chỉ đọc số liệu để vẽ, không crawl trong lúc build.

**Vì sao validator không chỉ đọc `manifests/stages.json`?** Nó chạy trước thời điểm manifest cuối được ghi. File đó có thể chưa có hoặc thuộc lần chạy trước. Checkpoint được ghi ngay sau từng stage nên phù hợp cho kiểm tra trong pipeline.

## 4. Validator kiểm tra gì, không bảo đảm điều gì?

- Đối chiếu version v1…vn và đường dẫn source/corpus đúng paper/version.
- Kiểm tra checkpoint identity, trạng thái, artifact tồn tại, đường dẫn không thoát output.
- Kiểm tra source khác rỗng, checksum SHA-256 nếu có trong download sidecar.
- Kiểm tra corpus có file `.tex`/`.bib` đọc được, phát hiện file rỗng/dấu hiệu binary và file thừa. Cho phép sidecar `extract_metadata.json`.
- So metadata với dữ liệu chuẩn hóa từ discovery; kiểm schema references, trường thiếu và reference trùng định danh.
- Ghi file trùng nội dung thành cảnh báo, không xóa hoặc gộp version.

**Giới hạn:** heuristic file không thay cho biên dịch LaTeX hoặc kiểm tra nội dung khoa học; checksum chỉ chứng minh bytes hiện tại khớp giá trị đã lưu, không tự chứng minh server đã gửi đầy đủ nếu giá trị gốc cũng được tính trên payload thiếu. Source trùng nội dung giữa version có thể hợp lệ.

**Câu dễ bị hỏi:** “Validator pass thì dữ liệu tải đủ chưa?” — Chưa thể kết luận. PDF-only có thể được ghi đúng và pass. Phải xem thêm discovery coverage, download/extract success, số source version và lỗi cụ thể.

## 5. Công thức thống kê và ví dụ tự tính

Các tỷ lệ lưu `numerator`, `denominator`, `value`. Nếu denominator=0 thì value=null. Null là không đủ cơ sở tính, không phải 0%.

| Chỉ số | Công thức/diễn giải |
|---|---|
| Discovery success | Paper discovery success / paper đầu vào duy nhất |
| Download success | Version download success / version biết từ discovery success |
| Extract success | Version extract success / version biết từ discovery success |
| Version record coverage | Version có checkpoint download đúng identity / version biết |
| Source-complete paper | Paper có mọi version download và extract success / paper đầu vào duy nhất |
| Validation pass | Paper có report valid / paper discovery success |
| Reference query success | Paper có JSON references đọc được với success/no_data / paper discovery success |
| Reference nonempty success | Paper success có reference / paper discovery success |
| References trung bình | Tổng refs / paper truy vấn được; no_data góp 0, not_indexed bị loại khỏi tập này và báo riêng |
| Reference metadata completeness | Ref đủ ref_arxiv_id/title/authors/date/S2 ID / tổng ref đọc được |

Ví dụ giả định: ba ID đầu vào; hai paper discovery được. Paper A có hai version source, B có một version PDF-only, C discovery lỗi. Có ba version **đã biết**, hai version tải source thành công: download success=2/3. Chỉ A đủ source mọi version: source-complete=1/3. Nếu A/B đều ghi trạng thái đúng, validation pass có thể=2/2. Không được nói “đã biết C có 0 version”.

Reference thiếu arXiv ID vẫn được giữ nội bộ. Trong công thức completeness đang dùng, nó không được tính đủ năm trường. Đây là định nghĩa nghiêm ngặt cần công bố, không có nghĩa mọi ref thiếu arXiv ID đều là crawler bug.

**Vì sao không đếm toàn thư mục corpus?** Nó có thể chứa các paper của run trước. Thống kê chọn paper từ manifest lượt hoàn tất và gộp alias theo ID duy nhất. Mọi số đếm cần ghi rõ đơn vị paper hay paper-version.

## 6. Kích thước trước và sau lọc

Download sidecar dùng `raw_size_bytes`: bytes package tải về, có thể là nén. Extract sidecar cũng có `raw_size_bytes`, nhưng nghĩa là tổng bytes file **sau giải nén trước lọc**, cùng `corpus_size_bytes` sau lọc. Tên giống nhau nhưng ngữ cảnh khác nhau.

Muốn đánh giá việc bỏ hình, so hai số **đã giải nén** trên cùng tập paper có đủ số đo mọi version. Package nén báo riêng. Ví dụ gói nén 1 MB có thể thành 10 MB trước lọc và 2 MB sau lọc: việc bỏ hình giảm 80% trên dữ liệu đã giải nén, dù corpus 2 MB lớn hơn gói nén 1 MB.

Kích thước corpus trong extraction sidecar đếm file `.tex`/`.bib`; disk cuối toàn output còn source, metadata, state, sidecars và logs, nên không bằng riêng corpus size.

## 7. Đo thời gian

`time.perf_counter()` là đồng hồ đơn điệu phù hợp đo khoảng thời gian. Lấy hiệu hai lần đọc; không dùng timestamp lịch để trừ runtime vì đồng hồ hệ thống có thể được chỉnh.

- Wall time toàn pipeline: bắt đầu subprocess đến lúc nó kết thúc/được quan sát kết thúc; gồm HTTP, retries, backoff, pacing, import, ghi file và validator. Cài môi trường không thuộc runtime pipeline.
- Timing discovery/stage/paper: observer ghi JSONL sau khi từng đơn vị hoàn tất. Paper timing gồm discovery và các version/module của paper/input.
- Cached flag: cho biết bước tái sử dụng checkpoint, không được quảng cáo số resume như tốc độ fresh crawl.
- Tổng stage time thường nhỏ hơn end-to-end wall time do còn startup, orchestration và tổng hợp snapshot.
- Nếu bị ngắt, paper đang chạy có thể chưa có event hoàn tất; không coi các event đã có là full runtime của mọi input.

Rate limiting làm runtime dài hơn nhưng là điều kiện truy cập hợp lệ; vẫn phải tính vào runtime thực tế. Không thể lấy tổng runtime unit tests rồi chia số fixture để tuyên bố runtime paper thật.

## 8. Đo RAM

Sampler dùng psutil lấy RSS của subprocess pipeline và descendants. Không lấy riêng RAM của notebook/sampler làm RAM crawler.

RSS(t)=tổng RSS của tiến trình pipeline và các child đang quan sát được. Peak=max các mẫu. Mean dùng giá trị mẫu trước giữ trong khoảng thời gian tới mẫu sau:

```text
mean_RSS = sum(RSS_i * (t_(i+1) - t_i)) / sum(t_(i+1) - t_i)
```

Ví dụ 100 MB trong 1 giây và 200 MB trong 3 giây → mean=175 MB, không phải 150 MB. Mẫu không đọc được phải được nhận diện bằng access errors/observed duration, không tự biến thành RAM 0.

**Giới hạn:** RSS cộng có thể đếm lại shared pages; peak ngắn giữa hai mẫu có thể bị bỏ lỡ; child tồn tại quá ngắn có thể không thấy. Chu kỳ mặc định 0,1 giây là thỏa hiệp độ chi tiết và overhead. Không khẳng định đo được đỉnh tuyệt đối.

## 9. Đo dung lượng đĩa

Đếm tổng logical file bytes trong output, gồm raw source, corpus, metadata/references, state/logs và staging/partial files. Results/samples nằm ngoài output để không tự làm phình chỉ số đang đo. Không theo symlink để tránh đếm ra ngoài cây dữ liệu.

Peak disk là cực đại quan sát trong lúc chạy; final là sau khi kết thúc. Peak có thể lớn hơn final vì file tạm đã được thu hồi. Logical bytes khác allocated blocks và khác dung lượng ổ đĩa toàn máy. Quét cây file cũng có overhead; giữ chu kỳ đo và cách đo nhất quán khi so sánh run.

## 10. Vì sao dùng subprocess và JSONL?

Subprocess tách đối tượng đo khỏi sampler. Wrapper quan sát cây tiến trình, giữ log và return code ngay cả khi pipeline lỗi. Nó không phải cơ chế làm crawler nhanh hơn.

JSONL lưu mỗi sample/event một dòng, có thể đọc phần đã ghi sau gián đoạn. `benchmark.json` tóm tắt lượt chạy, `statistics.json` mô tả dữ liệu, `pipeline.log` để chẩn đoán. Hai loại JSON tóm tắt không thay thế bằng chứng thô.

Environment gồm Python, packages, CPU/RAM, commit và dirty flag. Không ghi biến môi trường chứa token. Exporter loại local command/output/executable khỏi bản công khai, ghi SHA-256 file gốc trong provenance; giữ originals nội bộ để kiểm chứng.

## 11. Fresh và resume khác nhau như thế nào?

Fresh dùng output mới/rỗng. Resume dùng checkpoint để bỏ qua phần terminal còn artifact. Orchestrator không kiểm checksum nội dung khi reuse; muốn rà lại corpus thì chạy validator trực tiếp hoặc `statistics --revalidate`.

Downloader hiện còn cache nội bộ: `--force` ở orchestrator chưa đồng nghĩa bỏ mọi cache downloader. Vì vậy không hứa `--force` luôn tạo benchmark fresh. Dùng output mới là cách rõ ràng hiện tại. Config retry/timeout của discovery cũng không tự điều khiển downloader vì module có cấu hình riêng.

Không chạy hai pipeline cùng ghi một output. Sau ngắt, manifest có thể cũ so với checkpoint: hoàn tất resume trước khi tổng hợp một snapshot được coi là đầy đủ. Giữ nhãn và thư mục riêng cho mỗi run, không ghi đè kết quả.

## 12. Jupyter Book hoạt động ra sao?

Book dùng Jupyter Book 1.0.4.post1 cho tương thích `_config.yml` và `_toc.yml`. Jupyter Book 2 có CLI/config khác nên không cài lẫn tùy tiện.

`execute_notebooks: force` bắt chạy lại code cells khi build. Notebook đọc summary/samples và gọi pandas/matplotlib để sinh bảng/biểu đồ. `_toc.yml` quy định trang, workflow CI cài dependencies, build nghiêm ngặt rồi deploy HTML lên Pages. Không crawl trong build vì cần kết quả ổn định và tránh gọi API lại mỗi lần sửa văn bản.

`TV5_RUN` chọn thư mục summary. Thiếu run được chọn thì build lỗi, không lặng lẽ dùng demo. Có nhãn synthetic_demo ngay trên trang để người đọc hiểu nguồn con số.

Một repo source cho dự án; một repo book riêng cho MS1 và MS2. Việc tách task Git không có nghĩa tạo hai Jupyter Book hoặc loại hoàn toàn xung đột ở file chung.

## 13. Chuẩn bị demo vấn đáp

1. Mở `docs/TV5_MS1_GUIDE.md`, chỉ ra các file chính và pipeline.
2. Mở report HTML đã build; giải thích bảng success có tử số/mẫu số và nhãn demo.
3. Dùng tình huống PDF-only để giải thích validator pass khác download source success.
4. Mở một validation JSON, chỉ paper/version/path/code/message của lỗi.
5. Mở samples/timings và giải thích từ mẫu thô ra peak/mean/runtime; chỉ môi trường và commit.
6. Nêu giới hạn đo và phần cần nghiệm thu thật trên Colab, không khẳng định đã làm nếu mới có notebook.

## 14. Câu hỏi ngắn thường gặp

**Vì sao lưu mọi version?** Đề yêu cầu; MS2 cần dedup nội dung qua version. Chỉ lưu latest làm mất phạm vi kiểm tra.

**PDF-only có phải lỗi chương trình?** Không nhất thiết. Ghi no_source rõ ràng và giữ record; HTTP lỗi không được gán tùy tiện thành no_source.

**Vì sao file trùng chỉ cảnh báo?** Hai version có thể dùng cùng nội dung. QA ghi nhận, còn dedup chuẩn hóa là phạm vi MS2.

**SHA-256 có phát hiện source sai không?** Phát hiện thay đổi so với hash đã lưu; không kiểm chứng ngữ nghĩa hay chất lượng bytes ban đầu.

**Đã có đủ 25.000 paper chưa?** Excel giao 25.000 ID lookup. Phải chạy discovery mới biết ID tồn tại, version và độ bao phủ.

**RAM sampler có nằm trong RAM crawler không?** Không: đo subprocess pipeline và descendants, sampler ở tiến trình cha ngoài phạm vi.

**Mean RAM tại sao không lấy mean các mẫu?** Nếu thời gian giữa mẫu không đều do scan/scheduling, mean thường không phản ánh thời lượng; dùng trọng số thời gian.

**Thời gian tải trong sidecar có đủ benchmark không?** Không. Nó không bao gồm toàn discovery/extract/metadata/references/QA và overhead toàn pipeline.

**Vì sao không cộng tất cả status success?** Status thuộc nhiều đơn vị khác nhau: paper, version, stage. Cộng chung mất ý nghĩa mẫu số.

**No data có tính query thành công không?** Với định nghĩa hiện tại, no_data có JSON rỗng hợp lệ được tính query thành công nhưng không phải nonempty success; báo cả hai.

**Có thể nói streaming luôn RAM dưới 25 MB không?** Không. Tổng RAM phụ thuộc Python, thư viện, archive, extractor, validator và dữ liệu. Phải đo hệ thống thật.

**Book build được có nghĩa đã hoàn thành MS1 không?** Chưa. Còn nghiệm thu corpus, benchmark Colab CPU-only và website public.

## 15. Tự học theo thứ tự

Đọc pipeline/contracts → chạy test validator → tự tính tỷ lệ bằng ví dụ ba ID → đọc statistics → đọc observer/timings → đọc sampler/subprocess → build book → chạy Colab và thay số đo. Khi vấn đáp, giải thích mục đích và giới hạn trước, sau đó mới chỉ chi tiết hàm.

## 16. Trạng thái nghiệm thu và nguồn

- Code validator có trước; phần MS1 reporting, fixtures, notebooks và workflow đã được bổ sung trong nhiệm vụ hiện tại.
- Demo offline đã chạy, book đã build và thực thi notebook. Demo không phải benchmark Colab hay kết quả toàn assignment.
- Lượt smoke ban đầu bị WinError 10013 trong sandbox; thử lại có mạng đã thành công với `2508.12872`, một version và 97 references, validator pass. Số đo local này chưa phải Colab.
- Unittest 112 test qua; pytest 122 test và 38 subtest qua. Xem link/trạng thái publish cuối trong `TV5_MS1_KET_QUA.md`; không coi toàn assignment/Colab đã xong.

Nguồn: [đề MS1](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling/), [psutil](https://psutil.readthedocs.io/en/latest/), [perf_counter](https://docs.python.org/3/library/time.html#time.perf_counter), [Jupyter Book execution](https://jupyterbook.org/v1/content/execute.html), `docs/CONTRACTS.md`, code đã triển khai và báo cáo TV2. Đề/Moodle là căn cứ yêu cầu cuối cùng.
