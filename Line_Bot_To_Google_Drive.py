import sys
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os
import datetime
from dotenv import load_dotenv
from io import BytesIO
import time
from ssl import SSLError
import threading
from googleapiclient.http import MediaIoBaseDownload  # 用於下載 Google Drive 檔案

# ---------------------
# 取得基礎目錄：若打包成執行檔則使用 sys.executable 的目錄，否則使用 __file__
# ---------------------
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 設定本地存檔目錄：在 BASE_DIR 下建立一個 data 資料夾
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------
# 載入環境變數
# ---------------------
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
# 父資料夾ID，若未設定則為 None
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# 設定 Flask 靜態目錄為 DATA_DIR
app = Flask(__name__, static_folder=DATA_DIR)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------------------
# 全域變數定義
# ---------------------
reply_enabled = {}      # 是否回覆訊息開關，key 為對話來源ID
# 上傳記錄：{ key: { "images": [...], "files": [...], "videos": [...] } }
# 每個 record 格式：
#   { "name": <檔名>, "upload_time": <上傳時間>,
#     "local_link": <本地連結>, "cloud_link": <雲端連結>, "file_id": <雲端檔案ID> }
uploaded_files = {}
# 使用者自訂雲端資料夾設定（父資料夾）：{ key: <資料夾ID> }
user_drive_folder = {}
# 存儲開關設定：{ key: { "local": bool, "cloud": bool } }，預設為 local=True, cloud=False
storage_settings = {}
# 全域鎖，確保上傳／下載作業一次只處理一筆
upload_lock = threading.Lock()

# ---------------------
# Helper 函式：拆分長訊息發送
# ---------------------
# def send_long_message(reply_token, message, max_length=4000):
#     chunks = [message[i:i+max_length] for i in range(0, len(message), max_length)]
#     if len(chunks) > 5:
#         chunks = chunks[:5]
#         chunks[-1] += "\n[訊息過長，僅顯示部分內容]"
#     messages = [TextSendMessage(text=chunk) for chunk in chunks]
#     line_bot_api.reply_message(reply_token, messages)

# ---------------------
# Helper 函式：確保檔案名稱唯一
# ---------------------
def get_unique_uploaded_filename(category_list, filename):
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    existing_names = [record["name"] for record in category_list]
    while candidate in existing_names:
        candidate = f"{base}-{counter}{ext}"
        counter += 1
    return candidate

# ---------------------
# Helper 函式：本地存檔（傳回檔案存放的絕對路徑，不回傳 URL）
# ---------------------
def store_locally(data, file_name, category, group_name):
    local_dir = os.path.join(DATA_DIR, group_name, category)
    os.makedirs(local_dir, exist_ok=True)
    file_path = os.path.join(local_dir, file_name)
    with open(file_path, "wb") as f:
        f.write(data)
    return file_path


# ---------------------
# Helper 函式：在 Google Drive 建立子資料夾（若不存在則建立）
# ---------------------
def get_or_create_drive_subfolder(folder_name, parent_folder_id):
    query = f"mimeType = 'application/vnd.google-apps.folder' and trashed = false and name = '{folder_name}' and '{parent_folder_id}' in parents"
    response = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    folders = response.get('files', [])
    if folders:
        return folders[0]['id']
    else:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = drive_service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')

# ---------------------
# Google Drive 上傳相關（含重試機制）
# ---------------------
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ['https://www.googleapis.com/auth/drive']
credentials = service_account.Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

def upload_to_drive(stream, file_name, mime_type, folder_id=None, retry=5):
    for attempt in range(retry):
        try:
            file_metadata = {'name': file_name}
            if folder_id:
                file_metadata['parents'] = [folder_id]
            media = MediaIoBaseUpload(stream, mimetype=mime_type)
            uploaded_file = drive_service.files().create(
                body=file_metadata, media_body=media, fields='id'
            ).execute()
            drive_service.permissions().create(
                fileId=uploaded_file.get('id'),
                body={'type': 'anyone', 'role': 'reader'}
            ).execute()
            return uploaded_file.get('id')
        except SSLError as e:
            if attempt < retry - 1:
                time.sleep(5)
                stream.seek(0)
                continue
            else:
                raise e

def get_drive_file_link(file_id):
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

# ---------------------
# 輔助函式：取得群組與使用者名稱
# ---------------------
def get_group_name(event):
    if isinstance(event.source, SourceGroup):
        group_id = event.source.group_id
        try:
            group_summary = line_bot_api.get_group_summary(group_id)
            return group_summary.group_name
        except Exception as e:
            print(f"⚠️ 無法取得群組名稱，錯誤: {e}")
            return f"群組_{group_id}"
    return "個人聊天"

def get_user_name(event):
    try:
        if isinstance(event.source, SourceGroup):
            user_id = event.source.user_id
            group_id = event.source.group_id
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
        else:
            user_id = event.source.user_id
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return "未知用戶"

# ---------------------
# LINE Bot Webhook 處理
# ---------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ---------------------
# 處理文字訊息（指令）
# ---------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    key = event.source.group_id if isinstance(event.source, SourceGroup) else user_id
    # 初始化存儲設定，預設為本地 True, 雲端 False
    if key not in storage_settings:
        storage_settings[key] = {"local": True, "cloud": False}
    user_message = event.message.text.strip()
    
    if user_message == '@開啟訊息':
        reply_enabled[key] = True
        reply = TextSendMessage(text="✅ 已開啟回覆訊息。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == '@關閉訊息':
        reply_enabled[key] = False
        reply = TextSendMessage(text="❌ 已關閉回覆訊息。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == "@幫助":
        help_text = (
            "【機器人使用說明】\n\n"
            "【基本指令】\n"
            "  @開啟訊息：啟用自動回覆上傳結果。\n"
            "  @關閉訊息 ：停用自動回覆上傳結果。\n\n"
            "【存儲控制指令】\n"
            "  @開啟本地下載：啟用本地存檔（預設開啟），檔案將存放至伺服器內部（data 資料夾），不提供公開下載連結。\n"
            "  @關閉本地下載：停用本地存檔，上傳後不會存檔至本地。\n"
            "  @設定雲端資料夾 <資料夾ID>：設定上傳至 Google Drive 的目標父資料夾ID。\n"
            "  @開啟雲端上傳：啟用雲端上傳，檔案將上傳至 Google Drive 中，\n"
            "               系統會在指定父資料夾下建立以群組名稱命名的子資料夾，\n"
            "               再於該資料夾下建立 images、files、videos 子資料夾，\n"
            "               最後將檔案上傳至對應的子資料夾中。\n"
            "  @關閉雲端上傳 ：停用雲端上傳。\n\n"
            "【其他指令】\n"
            "  @幫助：顯示本使用說明資訊。\n\n"
            "【操作流程】\n"
            "  1. 初次使用時，請先執行 @開啟訊息 以啟用自動回覆。\n"
            "  2. 本地下載預設為開啟；雲端上傳預設為關閉。\n"
            "     若需雲端上傳，請先使用 @設定雲端資料夾 指令設定父資料夾ID，\n"
            "     系統會在該父資料夾下自動建立以群組名稱命名的子資料夾，再於該子資料夾下建立 images、files、videos 子資料夾，\n"
            "     最後將檔案上傳到對應的子資料夾中。\n"
            "  3. 上傳檔案後，系統會依據存儲設定分別進行本地存檔與／或雲端上傳，並回覆相應狀態：\n"
            "     - 若本地下載開啟，回覆提示「已存至本地」。\n"
            "     - 若雲端上傳開啟，回覆雲端連結。\n"
            "     - 若兩者皆關閉，回覆「目前本地下載與雲端上傳皆關閉」。\n"
        )
        reply = TextSendMessage(text=help_text)
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == "@開啟本地下載":
        storage_settings[key]["local"] = True
        reply = TextSendMessage(text="✅ 已開啟本地下載（存檔）。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == "@關閉本地下載":
        storage_settings[key]["local"] = False
        reply = TextSendMessage(text="✅ 已關閉本地下載。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == "@開啟雲端上傳":
        if key not in user_drive_folder or not user_drive_folder[key]:
            reply = TextSendMessage(text="❌ 尚未設定雲端資料夾ID，請先使用 @設定雲端資料夾 <資料夾ID> 指令設定。")
            line_bot_api.reply_message(event.reply_token, reply)
            return
        storage_settings[key]["cloud"] = True
        reply = TextSendMessage(text="✅ 已開啟雲端上傳。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message == "@關閉雲端上傳":
        storage_settings[key]["cloud"] = False
        reply = TextSendMessage(text="✅ 已關閉雲端上傳。")
        line_bot_api.reply_message(event.reply_token, reply)
    elif user_message.startswith("@設定雲端資料夾"):
        parts = user_message.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            reply = TextSendMessage(text="請提供要設定的資料夾ID，例如：@設定雲端資料夾 <資料夾ID>")
            line_bot_api.reply_message(event.reply_token, reply)
            return
        folder_id = parts[1].strip()
        user_drive_folder[key] = folder_id
        reply = TextSendMessage(text=f"✅ 已設定上傳至 Google Drive 的資料夾ID為：{folder_id}")
        line_bot_api.reply_message(event.reply_token, reply)


# ---------------------
# 處理圖片訊息（支援本地存儲與雲端上傳）
# ---------------------
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    with upload_lock:
        user_name = get_user_name(event)
        key = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.user_id
        group_name_val = get_group_name(event)
        # 取得父資料夾ID：使用者設定的雲端資料夾或預設值
        parent_folder_id = user_drive_folder.get(key, GOOGLE_DRIVE_FOLDER_ID)
        # 若雲端上傳啟用且父資料夾ID存在，則先在父資料夾下建立以群組名稱命名的子資料夾，
        # 再在該資料夾下建立 "images" 子資料夾
        if storage_settings.get(key, {}).get("cloud", False) and parent_folder_id:
            drive_group_folder = get_or_create_drive_subfolder(group_name_val, parent_folder_id)
            cloud_folder = get_or_create_drive_subfolder("images", drive_group_folder)
        else:
            cloud_folder = None
        image_id = event.message.id
        file_name = f"{user_name}-{image_id}.jpg"
        if key not in uploaded_files:
            uploaded_files[key] = {"images": [], "files": [], "videos": []}
        file_name = get_unique_uploaded_filename(uploaded_files[key]["images"], file_name)
        stream = BytesIO()
        image_content = line_bot_api.get_message_content(image_id)
        for chunk in image_content.iter_content():
            stream.write(chunk)
        stream.seek(0)
        data = stream.getvalue()
        local_link = ""
        cloud_link = ""
        if storage_settings.get(key, {}).get("local", True):
            local_dir = os.path.join(DATA_DIR, group_name_val, "images")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, file_name)
            with open(local_path, "wb") as f:
                f.write(data)
        if storage_settings.get(key, {}).get("cloud", False) and cloud_folder:
            new_stream = BytesIO(data)
            file_id_cloud = upload_to_drive(new_stream, file_name, 'image/jpeg', cloud_folder)
            cloud_link = get_drive_file_link(file_id_cloud)
        upload_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uploaded_files[key]["images"].append({
            "name": file_name,
            "upload_time": upload_time,
            "local_link": "",  # 本地連結不回覆
            "cloud_link": cloud_link,
            "file_id": file_id_cloud if cloud_link else ""
        })
        if reply_enabled.get(key, False):
            if not (storage_settings[key]["local"] or storage_settings[key]["cloud"]):
                msg = "目前本地下載與雲端上傳皆關閉。"
            else:
                msg_parts = []
                if storage_settings[key]["local"]:
                    msg_parts.append("圖片已存至本地")
                if storage_settings[key]["cloud"] and cloud_link:
                    msg_parts.append(f"雲端連結：{cloud_link}")
                msg = "\n".join(msg_parts)
            reply = TextSendMessage(text="📸 " + msg)
            line_bot_api.reply_message(event.reply_token, reply)

# ---------------------
# 處理檔案訊息（支援本地存儲與雲端上傳）
# ---------------------
@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    with upload_lock:
        user_name = get_user_name(event)
        key = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.user_id
        group_name_val = get_group_name(event)
        parent_folder_id = user_drive_folder.get(key, GOOGLE_DRIVE_FOLDER_ID)
        if storage_settings.get(key, {}).get("cloud", False) and parent_folder_id:
            drive_group_folder = get_or_create_drive_subfolder(group_name_val, parent_folder_id)
            cloud_folder = get_or_create_drive_subfolder("files", drive_group_folder)
        else:
            cloud_folder = None
        file_id_msg = event.message.id
        original_file_name = event.message.file_name
        file_name = f"{user_name}-{original_file_name}"
        if key not in uploaded_files:
            uploaded_files[key] = {"images": [], "files": [], "videos": []}
        file_name = get_unique_uploaded_filename(uploaded_files[key]["files"], file_name)
        stream = BytesIO()
        file_content = line_bot_api.get_message_content(file_id_msg)
        for chunk in file_content.iter_content():
            stream.write(chunk)
        stream.seek(0)
        data = stream.getvalue()
        mime_type = "application/octet-stream"
        if file_name.lower().endswith('.pdf'):
            mime_type = "application/pdf"
        local_link = ""
        cloud_link = ""
        if storage_settings.get(key, {}).get("local", True):
            local_dir = os.path.join(DATA_DIR, group_name_val, "files")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, file_name)
            with open(local_path, "wb") as f:
                f.write(data)
        if storage_settings.get(key, {}).get("cloud", False) and cloud_folder:
            new_stream = BytesIO(data)
            file_id_cloud = upload_to_drive(new_stream, file_name, mime_type, cloud_folder)
            cloud_link = get_drive_file_link(file_id_cloud)
        upload_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uploaded_files[key]["files"].append({
            "name": file_name,
            "upload_time": upload_time,
            "local_link": "",  # 本地連結不回覆
            "cloud_link": cloud_link,
            "file_id": file_id_cloud if cloud_link else ""
        })
        if reply_enabled.get(key, False):
            if not (storage_settings[key]["local"] or storage_settings[key]["cloud"]):
                msg = "目前本地下載與雲端上傳皆關閉。"
            else:
                msg_parts = []
                if storage_settings[key]["local"]:
                    msg_parts.append("檔案已存至本地")
                if storage_settings[key]["cloud"] and cloud_link:
                    msg_parts.append(f"雲端連結：{cloud_link}")
                msg = "\n".join(msg_parts)
            reply = TextSendMessage(text="📁 " + msg)
            line_bot_api.reply_message(event.reply_token, reply)

# ---------------------
# 處理影片訊息（支援本地存儲與雲端上傳）
# ---------------------
@handler.add(MessageEvent, message=VideoMessage)
def handle_video_message(event):
    with upload_lock:
        user_name = get_user_name(event)
        key = event.source.group_id if isinstance(event.source, SourceGroup) else event.source.user_id
        group_name_val = get_group_name(event)
        parent_folder_id = user_drive_folder.get(key, GOOGLE_DRIVE_FOLDER_ID)
        if storage_settings.get(key, {}).get("cloud", False) and parent_folder_id:
            drive_group_folder = get_or_create_drive_subfolder(group_name_val, parent_folder_id)
            cloud_folder = get_or_create_drive_subfolder("videos", drive_group_folder)
        else:
            cloud_folder = None
        video_id = event.message.id
        file_name = f"{user_name}-{video_id}.mp4"
        if key not in uploaded_files:
            uploaded_files[key] = {"images": [], "files": [], "videos": []}
        file_name = get_unique_uploaded_filename(uploaded_files[key]["videos"], file_name)
        stream = BytesIO()
        video_content = line_bot_api.get_message_content(video_id)
        for chunk in video_content.iter_content():
            stream.write(chunk)
        stream.seek(0)
        data = stream.getvalue()
        local_link = ""
        cloud_link = ""
        if storage_settings.get(key, {}).get("local", True):
            local_dir = os.path.join(DATA_DIR, group_name_val, "videos")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, file_name)
            with open(local_path, "wb") as f:
                f.write(data)
        if storage_settings.get(key, {}).get("cloud", False) and cloud_folder:
            new_stream = BytesIO(data)
            file_id_cloud = upload_to_drive(new_stream, file_name, 'video/mp4', cloud_folder)
            cloud_link = get_drive_file_link(file_id_cloud)
        upload_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uploaded_files[key]["videos"].append({
            "name": file_name,
            "upload_time": upload_time,
            "local_link": "",  # 本地連結不回覆
            "cloud_link": cloud_link,
            "file_id": file_id_cloud if cloud_link else ""
        })
        if reply_enabled.get(key, False):
            if not (storage_settings[key]["local"] or storage_settings[key]["cloud"]):
                msg = "目前本地下載與雲端上傳皆關閉。"
            else:
                msg_parts = []
                if storage_settings[key]["local"]:
                    msg_parts.append("影片已存至本地")
                if storage_settings[key]["cloud"] and cloud_link:
                    msg_parts.append(f"雲端連結：{cloud_link}")
                msg = "\n".join(msg_parts)
            reply = TextSendMessage(text="🎬 " + msg)
            line_bot_api.reply_message(event.reply_token, reply)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
