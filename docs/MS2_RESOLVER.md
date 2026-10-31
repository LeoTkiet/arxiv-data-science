# TV1 Resolver — phần B, Milestone 2

Module: `src/arxiv_pipeline/parsing/resolver.py`. Nhánh triển khai:
`feature/m2-tv1-resolver`, từ main đã merge PR9. Resolver tuân thủ
[MS2_CONTRACTS.md](MS2_CONTRACTS.md); không thay đổi Hierarchy của PR10 hoặc
schema bốn cột của bảng hierarchy. Đây là xử lý cú pháp tĩnh, không chạy TeX/shell.

## Giao tiếp với pipeline

```python
from pathlib import Path
from arxiv_pipeline.parsing.resolver import run

result = run(
    record, Path('ms2-output'), version=version_record,
    upstream={
        'context': {
            'input_root': str(Path('frozen-ms1-input').resolve()),
            'input_snapshot_id': snapshot_fingerprint,
        },
        'stages': {
            'download': download_checkpoint,
            'extract': extract_checkpoint,
        },
    },
)
```

`record`, `version_record` và hai checkpoint là dict theo hợp đồng MS1.
Snapshot/fingerprint do caller chuẩn bị; phần C sẽ điều phối và kiểm tra resume.
Không lấy status từ VersionRecord. Hai root phải tách rời (không trùng hoặc chứa
nhau), input chỉ đọc. Chạy trên bản sao MS1 đã hoàn tất, không corpus đang ghi.

Success/no_source ghi atomic `parsed/<paper_key>/vN/gathered.json`, trả
`StageResult(stage='resolve', schema_version='2.0')` với artifact `gathered`.
Mọi path serialize là đường dẫn tương đối, dùng `/`; source paths phân giải theo
input root, artifact path theo output root. Không serialize input_root tuyệt đối.
Errors/skipped không khai báo artifact success; file gathered cũ nếu có không
được consumer tái sử dụng khi kết quả mới không success/no_source.

## Cú pháp và lựa chọn root

- Chọn root có `\documentclass` không bị comment hoặc nằm trong literal/macro
  definition. Ưu tiên main.tex, paper.tex, ms.tex, tên ID bài; sau đó số include
  hợp lệ và thứ tự path. Nhiều ứng viên có cảnh báo `ambiguous_root`.
- Ghép `\input{path}`, `\input path`, `\include{path}` nhiều cấp; tự thêm `.tex`
  khi thiếu suffix. Tìm từ thư mục file chứa lệnh, sau đó corpus root. Cho phép
  `../` trong include nếu đường dẫn thực sự vẫn nằm trong corpus version; paths
  serialize đã chuẩn hóa nên không chứa `..`.
  Include nằm trong đối số tiêu đề như `\section{\input{title}}` cũng được ghép.
- Giữ thứ tự và mọi lần include lặp; stack của nhánh hiện tại chặn chu trình.
  `included_files` là danh sách duy nhất theo lần gặp đầu tiên, không gồm root.
  `ignored_files` liệt kê TeX chưa đi tới; không xóa source.
- Hỗ trợ `\includeonly{a,b}`: chỉ lọc include, không lọc input. Theo dõi
  `\endinput` theo file và dừng toàn luồng ở `\end{document}`.
- Cho phép begin/end document nằm trong file được include. Chỉ phần giữa cặp
  begin/end đưa vào body; preamble (sau ghép) lưu riêng, gồm macro definitions.
- Comment `%` và newline theo sau bị bỏ; `\%` giữ nguyên. Xét đúng parity khi
  có nhiều dấu backslash trước percent. Bảo vệ `\verb`, verbatim/verbatim*,
  lstlisting, minted; môi trường comment bị loại.
- Giữ nguyên macro definitions newcommand/renewcommand/providecommand/
  DeclareRobustCommand/newenvironment/renewenvironment/def/gdef/edef/xdef,
  không chạy include chỉ vì chúng nằm trong một định nghĩa chưa được gọi.
- Nếu gọi macro/environment điều khiển include hoặc document boundaries,
  trả `dynamic_include` thay vì báo success cho body thiếu.

## References và truy vết

- Bóc raw bibitem (giữ optional label và citation key) từ thebibliography hoặc
  heading References/Bibliography chứa bibitem; bỏ đúng phạm vi đó khỏi body.
  Heading kế tiếp cùng cấp/cao hơn kết thúc phạm vi References, giữ Appendix và
  Acknowledgements. Không loại `\cite{...}` khỏi body.
- Thu thập inventory `.bib`; nhận diện bibliography/addbibresource ở preamble
  và body để tạo referenced_bib_file_paths. Thiếu file `.bib` được khai báo có
  cảnh báo `missing_bibliography`; không tự tạo nội dung reference giả.
- Loại bibliography/bibliographystyle/addbibresource/printbibliography khỏi body.
  Không chuyển references thành BibTeX ở đây; đó là nhiệm vụ Bibliography TV4.
- Source spans phủ toàn bộ merged_body_tex, mỗi đoạn trỏ về path và khoảng
  `[source_start, source_end)` trong chuỗi gốc đã decode. Offsets là code points,
  **giữ CRLF**, không phải byte offsets hoặc text sau universal newline conversion.
  Include lặp có các span riêng, kể cả khi cùng source offsets.
- Thử decode UTF-8 trước, fallback Latin-1 có warning `encoding_fallback`.

## Trạng thái đầu vào và giới hạn

- Thiếu checkpoint hoặc upstream download/extract lỗi: skipped kèm ErrorInfo.
- Sai identity/schema/path, thiếu/rỗng source dù download success, corpus thiếu,
  corpus success nhưng rỗng, thiếu root/document boundary: data_error.
- PDF-only: chỉ nhận download no_source khi sidecar đúng paper/version, format
  pdf và HTTP 2xx; extract phải skipped/no_source. HTML không được coi là PDF-only.
- Extract no_source chỉ được nhận khi source tồn tại không rỗng, checkpoint có
  kind no_tex_source và lý do xác nhận archive không có TeX/BibTeX. Không chấp
  nhận lý do thiếu/rỗng file nguồn của extractor MS1 thành no_source MS2.
  Nếu checkpoint empty mâu thuẫn với corpus còn TeX/BibTeX, trả data_error.
- Corpus chỉ có BibTeX: no_source về body nhưng giữ bib_file_paths cho TV4.
- Lỗi include thiếu, vòng lặp, path ra ngoài version (kể cả symlink), malformed
  bibliography: data_error, không ghi artifact success một phần.
- TeX động (conditional compilation, catcode/csname, vòng lặp, import/subfile và
  dynamic filename) chưa hỗ trợ, trả data_error tường minh. Không phải bộ biên
  dịch LaTeX đầy đủ; macro văn bản được giữ nguyên để downstream xử lý.
- Giới hạn mặc định: 64 cấp include, 64 MiB source TeX đã đọc, 64 Mi ký tự sau
  mở rộng. Vượt giới hạn trả data_error. Giới hạn source áp dụng cả file được
  đọc để tìm root; không tính `.bib` vì resolver chỉ bàn giao đường dẫn `.bib`.
- Lỗi I/O trả error; atomic_json dùng helper chung của repo. Không sửa CLI,
  checkpoint/resume hay Orchestrator MS1/MS2 trong phần B.

## Kiểm thử và phần chưa nghiệm thu

```powershell
python -m pytest tests/parsing/test_resolver.py -q
python -m pytest -q --basetemp artifacts/pytest-resolver-<unique-id>
```

Tests tự tạo corpus/checkpoint trong thư mục tạm, không gọi mạng, không đưa corpus
thật vào Git. Trường hợp symlink được skip khi host Windows không cấp quyền tạo
symlink; kiểm tra escape bằng đường dẫn thường vẫn chạy. Chưa chạy corpus thật
đang được nhóm thu thập, chưa xác nhận tích hợp với Hierarchy PR10. Thứ tự ghép
pipeline và truyền envelope cho TV2 thuộc phần C.
