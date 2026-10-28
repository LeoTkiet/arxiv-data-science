# HƯỚNG DẪN THỰC THI PIPELINE THU THẬP DỮ LIỆU TRÊN GOOGLE COLAB (MILESTONE 1)
> **Môn học:** Nhập môn Khoa học Dữ liệu (Intro to Data Science) — Trường ĐHKHTN, ĐHQG-HCM  
> **Nhóm thực hiện:** Nhóm 6  
> **Notebook thực thi:** `notebooks/MS1_Pipeline_Colab_Runner.ipynb`  
> **Dải ID được giao:** 25.000 bài báo (chia đều cho 5 thành viên, mỗi người 5.000 bài)

---

## 1. BẢNG PHÂN CHIA DẢI ID VÀ TRÁCH NHIỆM CHẠY COLAB

Theo cấu hình chính thức từ giảng viên tại `configs/ms1_group6_ranges.json`, dải 25.000 bài của Nhóm 6 được phân chia cho 5 thành viên như sau:

| Thành viên | Vai trò | Dải ID được giao | Số lượng | Thư mục lưu trữ trên Google Drive |
| :--- | :--- | :--- | :---: | :--- |
| **TV1** | Leader & Discovery | `2508.12872:2508.17871` | 5.000 bài | `/content/drive/MyDrive/arxiv_ms1_data/tv1` |
| **TV2** | Source Downloader | `2506.15788:2506.20787` | 5.000 bài | `/content/drive/MyDrive/arxiv_ms1_data/tv2` |
| **TV3** | File Extractor | `2411.08799:2411.13798` | 5.000 bài | `/content/drive/MyDrive/arxiv_ms1_data/tv3` |
| **TV4** | Metadata & S2 References | `2410.18025:2410.23024` | 5.000 bài | `/content/drive/MyDrive/arxiv_ms1_data/tv4` |
| **TV5** | QA, Benchmark & Report | `2410.03025:2410.08024` | 5.000 bài | `/content/drive/MyDrive/arxiv_ms1_data/tv5` |

> ⚠️ **LƯU Ý QUAN TRỌNG:**  
> Mỗi dải 5.000 bài tốn khoảng **10 đến 12 tiếng** chạy liên tục do quy định giãn cách 3.0s của arXiv.  
> **5 thành viên bắt buộc phải chạy song song trên 5 tài khoản Google Colab riêng biệt**. Tuyệt đối không chạy dồn trên 1 máy vì sẽ mất gần 3 ngày liên tục và vượt quá giới hạn của Colab!

---

## 2. CÁC BƯỚC THIẾT LẬP VÀ THỰC THI TRÊN GOOGLE COLAB

### Bước 1: Mở Notebook trên Google Colab
1. Truy cập vào [Google Colab](https://colab.research.google.com/).
2. Chọn thẻ **Upload (Tải lên)** $\rightarrow$ Chọn tệp `notebooks/MS1_Pipeline_Colab_Runner.ipynb` từ repository.

### Bước 2: Kiểm tra cấu hình phần cứng (Bắt buộc CPU-only)
* Đề cương môn học quy định: *"The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode."*
* Trên thanh menu của Colab: Chọn **Runtime (Thời gian chạy)** $\rightarrow$ **Change runtime type (Thay đổi loại thời gian chạy)** $\rightarrow$ Chọn **CPU** $\rightarrow$ Bấm **Save (Lưu)**.

### Bước 3: Gắn kết Google Drive (Cell 1)
* Bấm chạy Cell 1 để kết nối tài khoản Google Drive cá nhân.
* Toàn bộ dữ liệu tải về (`corpus/`, `metadata/`, `references/`) sẽ được ghi trực tiếp vào Google Drive để **bảo toàn dữ liệu vĩnh viễn**, không bị mất khi phiên Colab hết hạn hoặc rớt mạng.

### Bước 4: Clone code và kiểm tra môi trường (Cell 2 & 3)
* Cell 3 sẽ tự động clone repository `arxiv-data-science`, cài đặt các package cần thiết (`.[reporting,dev]`) và chạy unit test nhanh để bảo đảm môi trường không bị lỗi.

### Bước 5: Chọn vai trò của bạn trên Form tương tác (Cell 5)
* Tại ô chọn `MEMBER_ROLE`, bấm vào menu thả xuống và chọn đúng tên của mình:
  * Ví dụ bạn là TV2: Chọn **`TV2`** (Hệ thống sẽ tự động nạp dải `2506.15788:2506.20787`).
  * Nếu muốn chạy thử nghiệm trước 1 bài xem hệ thống hoạt động ổn định hay không: Chọn **`TEST_SMOKE_1_BAI`**.
* Tùy chọn `ENABLE_BENCHMARK`: Mặc định chọn `True` để hệ thống vừa tải vừa đo đạc RAM và thời gian chạy phục vụ cho TV5.

### Bước 6: Bấm chạy Pipeline (Cell 6)
* Nhấn chạy Cell 6. Hệ thống sẽ tiến hành thu thập toàn bộ các phiên bản bài báo, tải TeX source, bóc tách metadata và trích xuất trích dẫn Semantic Scholar.

---

## 3. CƠ CHẾ KHÔI PHỤC KHI BỊ NGẮT KẾT NỐI (RESUME CAPABILITY)

* Google Colab phiên bản miễn phí có thể tự động ngắt kết nối sau vài tiếng hoặc khi máy tính rơi vào trạng thái ngủ (Sleep).
* **Đừng lo lắng!** Pipeline của nhóm đã được tích hợp cờ `--resume`.
* **Cách xử lý khi bị ngắt kết nối:**
  1. Mở lại notebook trên Colab.
  2. Chạy lại từ **Cell 1** (Mount Drive), **Cell 3** (Clone code), và **Cell 5** (Chọn role của bạn).
  3. Chạy lại **Cell 6**: Hệ thống sẽ đọc các checkpoint đã lưu trên Google Drive, **tự động bỏ qua các bài đã tải thành công** và chạy tiếp tục những bài còn lại mà không tốn công tải lại từ đầu!

---

## 4. MẸO GIỮ PHIÊN COLAB KHÔNG BỊ DISCONNECT (COLAB KEEP-ALIVE)

Để tránh việc Google Colab tự ngắt do treo máy không hoạt động khi đang chạy tác vụ dài:
1. Trên trình duyệt web Colab, nhấn phím `F12` (hoặc `Ctrl + Shift + I` trên Windows, `Cmd + Option + I` trên Mac) để mở **Developer Console**.
2. Chọn thẻ **Console**.
3. Dán đoạn mã sau vào và nhấn `Enter`:
```javascript
function ClickConnect(){
    console.log("Colab Keep-Alive: Dang giu ket noi..."); 
    document.querySelector("colab-connect-button")?.click() || document.querySelector("#connect")?.click();
}
setInterval(ClickConnect, 60000); // 1 phut click gia lap 1 lan
```

---

## 5. NGHIỆM THU VÀ BÀN GIAO KẾT QUẢ CHO TV5 (CELL 7 & 8)

Sau khi tiến trình chạy xong 5.000 bài:
1. **Chạy Cell 7 (Audit):** Kiểm tra xem trong dải 5.000 bài của bạn, có bao nhiêu bài đã hoàn thành hợp lệ, bao nhiêu bài là PDF-only, và có lỗi nào xảy ra không.
2. **Chạy Cell 8 (Đóng gói Artifacts):** Hệ thống sẽ tự động nén các tệp báo cáo hiệu năng (`benchmark.json`, `statistics.json`, `samples.jsonl`, `timings.jsonl`) thành tệp `TV_<tên_bạn>_results.zip` đặt trên Google Drive.
3. **Bàn giao:** Gửi tệp zip này cho **TV5** để TV5 import vào repository báo cáo Jupyter Book trên GitHub Pages!
