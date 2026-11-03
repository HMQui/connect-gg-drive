import asyncio
import base64
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Tải cấu hình từ file .env
load_dotenv()

# ==============================================================================
# CẤU HÌNH HỆ THỐNG TỪ BIẾN MÔI TRƯỜNG
# ==============================================================================
PORT = int(os.getenv("PORT", "8000"))
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "my_super_secret_postman_key")
LOCAL_SYNC_DIR = Path(os.getenv("LOCAL_SYNC_DIR", "./data/gdrive_sync")).resolve()

# Chế độ xác thực: "auto" | "service_account" | "oauth2"
AUTH_METHOD = os.getenv("AUTH_METHOD", "auto").strip().lower()

# Cấu hình Cách 1: Service Account
GDRIVE_SA_JSON_PATH = os.getenv("GDRIVE_SA_JSON_PATH", "")
GDRIVE_SA_JSON_BASE64 = os.getenv("GDRIVE_SA_JSON_BASE64", "")
GDRIVE_TEAM_DRIVE_ID = os.getenv("GDRIVE_TEAM_DRIVE_ID", "")

# Cấu hình Cách 2: OAuth 2.0 (Drive cá nhân)
GDRIVE_OAUTH_TOKEN = os.getenv("GDRIVE_OAUTH_TOKEN", "").strip()
GDRIVE_OAUTH_CLIENT_ID = os.getenv("GDRIVE_OAUTH_CLIENT_ID", "").strip()
GDRIVE_OAUTH_CLIENT_SECRET = os.getenv("GDRIVE_OAUTH_CLIENT_SECRET", "").strip()

# Target Folder ID (Dùng chung cho cả 2 cách)
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "").strip()

# File tạm cho SA JSON giải mã từ Base64
TEMP_SA_DIR = Path("./credentials").resolve()
TEMP_SA_FILE = TEMP_SA_DIR / "sa_decoded.json"

RESOLVED_SA_PATH: Optional[Path] = None


def get_rclone_binary() -> str:
    """Tự động tìm kiếm binary rclone trong thư mục hiện tại hoặc trong PATH hệ thống"""
    candidates = [
        Path("./rclone.exe").resolve(),
        Path("./rclone").resolve(),
        Path("./bin/rclone.exe").resolve(),
        Path("./bin/rclone").resolve(),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    system_rclone = shutil.which("rclone")
    if system_rclone:
        return system_rclone
    return "rclone"


# ==============================================================================
# IN-MEMORY STATE & CONCURRENCY LOCK
# ==============================================================================
task_lock = asyncio.Lock()

state: Dict[str, Any] = {
    "status": "idle",  # "idle" | "running" | "completed" | "failed"
    "current_action": None,  # "pull" | "push" | None
    "params": {},
    "started_at": None,
    "finished_at": None,
    "latest_stats": None,
    "recent_logs": [],
    "exit_code": None,
    "error_message": None,
}


def add_log_line(line: str) -> None:
    """Lưu trữ tối đa 20 dòng log gần nhất để tra cứu tiến độ"""
    clean_line = line.strip()
    if not clean_line:
        return
    state["latest_stats"] = clean_line
    logs: List[str] = state["recent_logs"]
    logs.append(clean_line)
    if len(logs) > 30:
        logs.pop(0)


# ==============================================================================
# SCHEMAS
# ==============================================================================
class PullRequest(BaseModel):
    remote_subpath: Optional[str] = Field(
        default="",
        description="Đường dẫn thư mục con trên Google Drive (tương đối so với GDRIVE_FOLDER_ID)",
    )
    local_subpath: Optional[str] = Field(
        default="",
        description="Đường dẫn thư mục con cục bộ (tương đối so với LOCAL_SYNC_DIR)",
    )


class PushRequest(BaseModel):
    local_subpath: Optional[str] = Field(
        default="",
        description="Đường dẫn file/thư mục cục bộ cần đẩy lên (tương đối so với LOCAL_SYNC_DIR)",
    )
    remote_subpath: Optional[str] = Field(
        default="",
        description="Đường dẫn thư mục con trên Google Drive nhận file",
    )


# ==============================================================================
# TIỆN ÍCH XÁC THỰC VÀ BẢO MẬT ĐƯỜNG DẪN (PATH TRAVERSAL DEFENSE)
# ==============================================================================
def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> None:
    """Kiểm tra header X-API-Key đối chiếu với API_SECRET_KEY trong .env"""
    if not x_api_key or x_api_key != API_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Header 'X-API-Key' không hợp lệ hoặc bị thiếu.",
        )


def sanitize_local_path(subpath: Optional[str]) -> Path:
    """
    Chuẩn hóa và ngăn chặn triệt để tấn công Path Traversal (../)
    Đảm bảo đường dẫn luôn nằm gọn trong thư mục LOCAL_SYNC_DIR.
    """
    base_dir = LOCAL_SYNC_DIR
    if not subpath or not subpath.strip():
        return base_dir

    clean_subpath = subpath.strip().lstrip("/\\")
    target_path = (base_dir / clean_subpath).resolve()

    # Kiểm tra xem target_path có nằm trong base_dir không
    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Security Alert: Phát hiện đường dẫn không an toàn hoặc vượt ngoài phạm vi thư mục cho phép ('{subpath}').",
        )

    return target_path


def sanitize_remote_subpath(subpath: Optional[str]) -> str:
    """
    Chuẩn hóa đường dẫn tương đối trên Google Drive (gdrive:subpath)
    """
    if not subpath or not subpath.strip():
        return ""
    clean = subpath.strip().replace("\\", "/").strip("/")
    # Không cho phép chứa các thành phần traversal
    parts = clean.split("/")
    if any(p == ".." for p in parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Security Alert: remote_subpath không được chứa '..'",
        )
    return clean


def is_oauth_mode() -> bool:
    """Xác định hệ thống đang dùng OAuth2 hay Service Account"""
    if AUTH_METHOD == "oauth2":
        return True
    if AUTH_METHOD == "service_account":
        return False
    # Auto-detect: nếu có GDRIVE_OAUTH_TOKEN thì ưu tiên OAuth2
    return bool(GDRIVE_OAUTH_TOKEN)


def build_rclone_env() -> Dict[str, str]:
    """
    Sinh biến môi trường chuẩn cho Rclone (Stateless - không cần rclone.conf)
    Hỗ trợ 2 chế độ:
      - Cách 1: Service Account (Google Workspace / Shared Drive / Read-only)
      - Cách 2: OAuth 2.0 (Drive cá nhân @gmail.com - Push/Pull không giới hạn)
    """
    global RESOLVED_SA_PATH
    env = os.environ.copy()
    env["RCLONE_CONFIG_GDRIVE_TYPE"] = "drive"
    env["RCLONE_CONFIG_GDRIVE_SCOPE"] = "drive"

    if is_oauth_mode():
        if not GDRIVE_OAUTH_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Chưa cấu hình GDRIVE_OAUTH_TOKEN trong .env cho chế độ OAuth 2.0.",
            )
        env["RCLONE_CONFIG_GDRIVE_TOKEN"] = GDRIVE_OAUTH_TOKEN
        if GDRIVE_OAUTH_CLIENT_ID:
            env["RCLONE_CONFIG_GDRIVE_CLIENT_ID"] = GDRIVE_OAUTH_CLIENT_ID
        if GDRIVE_OAUTH_CLIENT_SECRET:
            env["RCLONE_CONFIG_GDRIVE_CLIENT_SECRET"] = GDRIVE_OAUTH_CLIENT_SECRET
    else:
        if not RESOLVED_SA_PATH or not RESOLVED_SA_PATH.is_file():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Chưa cấu hình Service Account JSON hợp lệ trong .env (kiểm tra GDRIVE_SA_JSON_PATH hoặc GDRIVE_SA_JSON_BASE64).",
            )
        env["RCLONE_CONFIG_GDRIVE_SERVICE_ACCOUNT_FILE"] = str(RESOLVED_SA_PATH)
        if GDRIVE_TEAM_DRIVE_ID:
            env["RCLONE_CONFIG_GDRIVE_TEAM_DRIVE"] = GDRIVE_TEAM_DRIVE_ID

    if GDRIVE_FOLDER_ID:
        env["RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID"] = GDRIVE_FOLDER_ID

    return env


# ==============================================================================
# BACKGROUND RUNNER CHO RCLONE
# ==============================================================================
async def execute_rclone_task(
    action: str,
    source: str,
    destination: str,
    params: Dict[str, Any],
) -> None:
    """
    Chạy tiến trình rclone ngầm qua subprocess, theo dõi stream stdout/stderr real-time
    """
    state["status"] = "running"
    state["current_action"] = action
    state["params"] = params
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    state["finished_at"] = None
    state["latest_stats"] = "Đang khởi tạo tiến trình rclone..."
    state["recent_logs"] = []
    state["exit_code"] = None
    state["error_message"] = None

    rclone_bin = get_rclone_binary()
    rclone_cmd = [
        rclone_bin,
        "copy",
        source,
        destination,
        # Tối ưu cho file dung lượng lớn
        "--drive-chunk-size", "128M",
        "--transfers", "4",
        "--checkers", "8",
        "--retries", "3",
        "--low-level-retries", "10",
        "--drive-stop-on-upload-limit",
        "--stats", "2s",
        "--stats-one-line",
        "-v",
    ]

    try:
        rclone_env = build_rclone_env()
    except Exception as exc:
        state["status"] = "failed"
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        state["error_message"] = f"Lỗi nạp cấu hình Rclone: {str(exc)}"
        return

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *rclone_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=rclone_env,
        )

        # Đọc stream stdout theo thời gian thực
        if process.stdout:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break
                line_text = line_bytes.decode("utf-8", errors="replace").strip()
                if line_text:
                    add_log_line(line_text)

        await process.wait()
        exit_code = process.returncode
        state["exit_code"] = exit_code

        if exit_code == 0:
            state["status"] = "completed"
            state["latest_stats"] = "Hoàn tất truyền dữ liệu thành công 100%."
        else:
            state["status"] = "failed"
            state["error_message"] = (
                f"Tiến trình rclone kết thúc với mã lỗi {exit_code}. Xem recent_logs để biết chi tiết."
            )

    except FileNotFoundError:
        state["status"] = "failed"
        state["error_message"] = (
            "Không tìm thấy lệnh 'rclone' trên hệ thống. Hãy chạy script setup_rclone.sh để cài đặt."
        )
    except Exception as exc:
        state["status"] = "failed"
        state["error_message"] = f"Ngoại lệ khi thực thi rclone: {str(exc)}"
    finally:
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


# ==============================================================================
# LIFESPAN & STARTUP SETUP
# ==============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global RESOLVED_SA_PATH

    # Tạo thư mục đồng bộ local nếu chưa có
    LOCAL_SYNC_DIR.mkdir(parents=True, exist_ok=True)

    if is_oauth_mode():
        print("[INIT] Chế độ xác thực: OAuth 2.0 (Refresh Token - Google Drive cá nhân)")
        if not GDRIVE_OAUTH_TOKEN:
            print("[CẢNH BÁO] Chưa tìm thấy GDRIVE_OAUTH_TOKEN trong .env!")
    else:
        print("[INIT] Chế độ xác thực: Service Account (Google Workspace / Shared Drive)")
        # 1. Kiểm tra Base64 Service Account JSON trước
        if GDRIVE_SA_JSON_BASE64:
            TEMP_SA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                decoded_bytes = base64.b64decode(GDRIVE_SA_JSON_BASE64)
                TEMP_SA_FILE.write_bytes(decoded_bytes)
                RESOLVED_SA_PATH = TEMP_SA_FILE
                print(f"[INIT] Đã giải mã Service Account JSON từ Base64 ra: {RESOLVED_SA_PATH}")
            except Exception as e:
                print(f"[CẢNH BÁO] Không thể giải mã Base64 GDRIVE_SA_JSON_BASE64: {e}")

        # 2. Nếu không có Base64, kiểm tra đường dẫn trực tiếp
        if not RESOLVED_SA_PATH and GDRIVE_SA_JSON_PATH:
            path_candidate = Path(GDRIVE_SA_JSON_PATH).resolve()
            if path_candidate.is_file():
                RESOLVED_SA_PATH = path_candidate
                print(f"[INIT] Sử dụng file Service Account JSON tại: {RESOLVED_SA_PATH}")
            else:
                print(f"[CẢNH BÁO] File Service Account JSON không tồn tại tại: {path_candidate}")

    # 3. Kiểm tra rclone binary
    rclone_path = get_rclone_binary()
    if Path(rclone_path).is_file() or shutil.which(rclone_path):
        print(f"[INIT] Rclone binary sẵn sàng tại: {rclone_path}")
    else:
        print("[CẢNH BÁO] 'rclone' chưa được cài trong PATH. Hãy chạy ./setup_rclone.sh hoặc tải rclone.exe!")

    yield

    # Cleanup khi shutdown server (nếu sinh file tạm từ base64)
    if TEMP_SA_FILE.is_file() and GDRIVE_SA_JSON_BASE64:
        try:
            TEMP_SA_FILE.unlink()
        except OSError:
            pass


app = FastAPI(
    title="Google Drive High-Speed Sync API",
    description="Stateless High-Performance File Sync via Rclone and Google Service Account",
    version="1.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# API ENDPOINTS
# ==============================================================================
@app.get("/", tags=["Health"])
async def root():
    rclone_path = get_rclone_binary()
    is_ready = Path(rclone_path).is_file() or (shutil.which(rclone_path) is not None)
    return {
        "service": "Google Drive Rclone Sync API",
        "status": "healthy",
        "auth_mode": "oauth2" if is_oauth_mode() else "service_account",
        "local_sync_dir": str(LOCAL_SYNC_DIR),
        "rclone_installed": is_ready,
        "rclone_binary": rclone_path,
    }


@app.api_route("/api/status", methods=["GET", "POST"], tags=["Sync Control"])
async def get_status(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """
    Kiểm tra trạng thái tác vụ hiện tại, xem % tiến độ, tốc độ MB/s và mã lỗi.
    Hỗ trợ cả GET và POST để tiện thao tác trên Postman.
    """
    verify_api_key(x_api_key)
    return JSONResponse(status_code=status.HTTP_200_OK, content=state)


@app.post("/api/pull", tags=["Sync Control"])
async def pull_from_gdrive(
    payload: Optional[PullRequest] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Kéo dữ liệu từ Google Drive về Local Server (Asynchronous Non-blocking).
    Trả về 202 Accepted ngay lập tức hoặc 409 Conflict nếu đang có tác vụ khác chạy.
    """
    verify_api_key(x_api_key)

    if payload is None:
        payload = PullRequest()

    local_target = sanitize_local_path(payload.local_subpath)
    local_target.mkdir(parents=True, exist_ok=True)

    remote_clean = sanitize_remote_subpath(payload.remote_subpath)
    source_remote = f"gdrive:{remote_clean}" if remote_clean else "gdrive:"
    dest_local = str(local_target)

    # Concurrency Lock: Kiểm tra xem có đang chạy tác vụ nào không
    if task_lock.locked() or state["status"] == "running":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "message": "Hệ thống đang bận thực thi một tác vụ rclone khác. Vui lòng chờ tác vụ hiện tại hoàn tất.",
                "current_state": state,
            },
        )

    # Khởi chạy non-blocking task
    async def task_wrapper():
        async with task_lock:
            await execute_rclone_task(
                action="pull",
                source=source_remote,
                destination=dest_local,
                params={
                    "remote_source": source_remote,
                    "local_destination": dest_local,
                },
            )

    asyncio.create_task(task_wrapper())

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "Đã tiếp nhận yêu cầu PULL từ Google Drive thành công. Tác vụ đang chạy ngầm.",
            "action": "pull",
            "source": source_remote,
            "destination": dest_local,
            "check_status_endpoint": "/api/status",
        },
    )


@app.post("/api/push", tags=["Sync Control"])
async def push_to_gdrive(
    payload: Optional[PushRequest] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Đẩy dữ liệu từ Local Server lên Google Drive (Asynchronous Non-blocking).
    Trả về 202 Accepted ngay lập tức hoặc 409 Conflict nếu đang có tác vụ khác chạy.
    """
    verify_api_key(x_api_key)

    if payload is None:
        payload = PushRequest()

    local_source = sanitize_local_path(payload.local_subpath)
    if not local_source.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Đường dẫn nguồn cục bộ không tồn tại: {local_source}",
        )

    remote_clean = sanitize_remote_subpath(payload.remote_subpath)
    dest_remote = f"gdrive:{remote_clean}" if remote_clean else "gdrive:"
    src_local = str(local_source)

    # Concurrency Lock: Kiểm tra xem có đang chạy tác vụ nào không
    if task_lock.locked() or state["status"] == "running":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "message": "Hệ thống đang bận thực thi một tác vụ rclone khác. Vui lòng chờ tác vụ hiện tại hoàn tất.",
                "current_state": state,
            },
        )

    # Khởi chạy non-blocking task
    async def task_wrapper():
        async with task_lock:
            await execute_rclone_task(
                action="push",
                source=src_local,
                destination=dest_remote,
                params={
                    "local_source": src_local,
                    "remote_destination": dest_remote,
                },
            )

    asyncio.create_task(task_wrapper())

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "message": "Đã tiếp nhận yêu cầu PUSH lên Google Drive thành công. Tác vụ đang chạy ngầm.",
            "action": "push",
            "source": src_local,
            "destination": dest_remote,
            "check_status_endpoint": "/api/status",
        },
    )


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
