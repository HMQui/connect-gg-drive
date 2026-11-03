# Google Drive High-Speed Sync API Server (FastAPI + Rclone)

API Server hiệu năng cao viết bằng Python (FastAPI + Uvicorn) tích hợp công cụ truyền tải tệp **Rclone** để truyền dữ liệu dung lượng lớn (GB/TB) với Google Drive hoàn toàn bất đồng bộ (Non-blocking, Stateless, không cần file `rclone.conf` thủ công).

---

## Bảng so sánh lựa chọn phương thức xác thực

Trước khi cấu hình, hãy chọn 1 trong 2 phương thức xác thực phù hợp với loại tài khoản Google của bạn:

| Tiêu chí | CÁCH 1: Service Account (JSON Key) | CÁCH 2: OAuth 2.0 (Refresh Token) |
| :--- | :--- | :--- |
| **Phù hợp nhất cho** | **Google Workspace / Shared Drive** hoặc chỉ **Tải về (PULL)** | **Google Drive cá nhân (`@gmail.com`)** |
| **Tải lên (PUSH)** | ❌ Bị lỗi 403 quota trên Drive cá nhân<br>✅ Hoạt động tốt trên Shared Drive | ✅ **Hoạt động hoàn hảo** (dùng quota 15GB hoặc Google One của bạn) |
| **Tải về (PULL)** | ✅ Hoạt động tốt | ✅ Hoạt động tốt |
| **Độ tiện lợi** | Dùng file JSON tĩnh | Login trình duyệt 1 lần duy nhất lấy chuỗi token |

---

# PHẦN A: CẤU HÌNH THEO CÁCH 1 — SERVICE ACCOUNT
> **Khuyên dùng khi:** Bạn có Bộ nhớ dùng chung (Shared Drive / Team Drive) của Google Workspace / Edu, hoặc server chỉ có nhu cầu đọc/kéo dữ liệu về (PULL).

### Bước 1: Tạo Service Account và lấy file JSON Key
1. Truy cập [Google Cloud Console](https://console.cloud.google.com/).
2. Chọn dự án của bạn (ví dụ: `easysport`).
3. Vào **APIs & Services** > **Library** > tìm và **Enable** dịch vụ **Google Drive API**.
4. Vào **IAM & Admin** > **Service Accounts** > Bấm **Create Service Account**:
   - Đặt tên Service Account (ví dụ: `gdrive-fastapi-sync`).
   - Bấm **Create and Continue** rồi **Done**.
5. Bấm vào Service Account vừa tạo > chọn tab **Keys** > **Add Key** > **Create new key** > chọn kiểu **JSON** để tải file về máy.
6. Copy email của Service Account (dạng: `xxx@ten-du-an.iam.gserviceaccount.com`).
7. Đặt file vừa tải vào thư mục dự án: `./credentials/service-account.json`.

### Bước 2: Chia sẻ thư mục Google Drive cho Email Service Account
1. Mở Google Drive trên trình duyệt, mở thư mục bạn muốn đồng bộ.
2. Bấm nút **Chia sẻ (Share)** > dán email của Service Account ở Bước 1 vào.
3. Cấp quyền **Người chỉnh sửa (Editor)** (hoặc *Người quản lý nội dung* nếu là Shared Drive).
4. Bỏ chọn ô *"Thông báo cho mọi người"* rồi bấm **Chia sẻ**.
5. Lấy **Folder ID** trên thanh địa chỉ URL:
   ```text
   https://drive.google.com/drive/folders/1u3ZFTWVvbRwXwFfCvjMcpBPxfxuDwZFK
   ```
   👉 Giá trị sau `folders/` chính là Folder ID (`1u3ZFTWVvbRwXwFfCvjMcpBPxfxuDwZFK`).

### Bước 3: Điền vào `.env`
Mở file `.env` và điền phần **CÁCH 1**:
```env
# Chọn chế độ hoặc để auto
AUTH_METHOD=service_account

# Thư mục dữ liệu local
LOCAL_SYNC_DIR=./data/gdrive_sync
GDRIVE_FOLDER_ID=1u3ZFTWVvbRwXwFfCvjMcpBPxfxuDwZFK

# Thông tin Service Account
GDRIVE_SA_JSON_PATH=./credentials/service-account.json

# Nếu dùng Shared Drive (Bộ nhớ dùng chung) thì điền ID Shared Drive vào đây, nếu là Drive cá nhân thì ĐỂ TRỐNG:
GDRIVE_TEAM_DRIVE_ID=
```

---

# PHẦN B: CẤU HÌNH THEO CÁCH 2 — OAUTH 2.0 REFRESH TOKEN
> **Khuyên dùng khi:** Bạn dùng tài khoản **Google Drive cá nhân (`@gmail.com`)** và cần đẩy file (PUSH) lên Drive mà không bị lỗi hạn mức 0 byte của Google.

Cơ chế này sử dụng `refresh_token` đại diện cho chính tài khoản Gmail của bạn. Bạn **chỉ cần xác thực 1 lần duy nhất trên trình duyệt**, sau đó server sẽ tự động làm mới token vĩnh viễn, không bao giờ phải đăng nhập lại.

### Bước 1: Sinh chuỗi OAuth Token bằng Rclone
Mở Terminal / PowerShell tại thư mục dự án (nơi có sẵn file `rclone.exe`) và chạy lệnh:

- **Trên Windows (PowerShell / CMD):**
  ```powershell
  .\rclone.exe authorize "drive"
  ```
- **Trên Linux / macOS:**
  ```bash
  rclone authorize "drive"
  ```

### Bước 2: Đăng nhập cấp quyền trên trình duyệt
1. Sau khi chạy lệnh trên, trình duyệt web mặc định của bạn sẽ tự động mở ra trang đăng nhập Google.
2. Chọn tài khoản Google cá nhân của bạn.
3. Nếu thấy cảnh báo *"Google hasn't verified this app (Google chưa xác minh ứng dụng này)"*:
   - Bấm vào chữ **Advanced (Nâng cao)** ở góc dưới.
   - Chọn tiếp **Go to rclone (unsafe) / Tiếp tục truy cập rclone**.
4. Bấm **Allow (Cho phép)** để cấp quyền truy cập Google Drive.
5. Khi trình duyệt báo *"Success!"*, hãy quay lại màn hình Terminal.

### Bước 3: Lấy chuỗi Token và dán vào `.env`
1. Tại Terminal, bạn sẽ thấy Rclone in ra một đoạn JSON có cấu trúc dạng:
   ```json
   {"access_token":"ya29.a0...","token_type":"Bearer","refresh_token":"1//04...","expiry":"2026-11-03T12:00:00Z"}
   ```
2. Hãy copy **toàn bộ dòng JSON** đó (bao gồm cả dấu `{` và `}`).
3. Mở file `.env` và dán vào biến `GDRIVE_OAUTH_TOKEN`:
   ```env
   # Chọn chế độ hoặc để auto
   AUTH_METHOD=oauth2

   # ID thư mục đích trên Google Drive
   GDRIVE_FOLDER_ID=1u3ZFTWVvbRwXwFfCvjMcpBPxfxuDwZFK

   # Dán chuỗi JSON lấy được ở bước trên vào đây:
   GDRIVE_OAUTH_TOKEN={"access_token":"ya29...","token_type":"Bearer","refresh_token":"1//04...","expiry":"2026-..."}
   ```

---

## Hướng dẫn khởi chạy API Server

### 1. Cài đặt thư viện
```bash
pip install -r requirements.txt
```

### 2. Khởi chạy Server
```bash
python main.py
```
*(Mặc định server sẽ lắng nghe tại `http://localhost:8000`).*

---

## Hướng dẫn sử dụng Postman & cURL

Tất cả các API được bảo vệ bởi Header:
- **`X-API-Key`**: Giá trị khớp với `API_SECRET_KEY` trong `.env` (mặc định: `my_super_secret_postman_key`).

### 1. Kiểm tra trạng thái & tiến độ (`GET /api/status`)
- **Method:** `GET` (hoặc `POST`)
- **URL:** `http://localhost:8000/api/status`
- **Header:** `X-API-Key: my_super_secret_postman_key`
- **Ý nghĩa:** Trả về trạng thái tiến trình ngầm (`idle`, `running`, `completed`, `failed`), %, tốc độ truyền tải MB/s và ETA.

### 2. Tải file từ Google Drive về Server (`POST /api/pull`)
- **Method:** `POST`
- **URL:** `http://localhost:8000/api/pull`
- **Header:** `X-API-Key: my_super_secret_postman_key`, `Content-Type: application/json`
- **Body (JSON):**
  ```json
  {
    "remote_subpath": "CV_HuynhMinhQui.pdf",
    "local_subpath": ""
  }
  ```
  *(Để trống `"remote_subpath": ""` nếu muốn tải toàn bộ thư mục).*
- **Phản hồi:** Trả về `202 Accepted` ngay lập tức để không bị timeout trên Postman.

### 3. Tải file từ Server lên Google Drive (`POST /api/push`)
- **Method:** `POST`
- **URL:** `http://localhost:8000/api/push`
- **Header:** `X-API-Key: my_super_secret_postman_key`, `Content-Type: application/json`
- **Body (JSON):**
  ```json
  {
    "local_subpath": "test_upload.txt",
    "remote_subpath": ""
  }
  ```
- **Phản hồi:** Trả về `202 Accepted` (hoặc `409 Conflict` nếu đang có tác vụ khác đang chạy).
