from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os
from dotenv import load_dotenv
import datetime

# 載入環境變數
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 用於跟蹤是否開啟回復功能
reply_enabled = {}

# 檢查檔名是否已存在，若存在則在檔名後面加上 -數字
def get_unique_filename(directory, filename):
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base}-{counter}{ext}"
        counter += 1
    return candidate

# 取得群組名稱（若為群組則使用 LINE API 取得群組名稱）
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

# 取得用戶名稱（支援群組內成員），回傳使用者的顯示名稱
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

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 處理文字訊息，新增 @列表、@刪除 與 @關鍵字 功能
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    # 群組則以群組ID作為 key，否則以個人ID作為 key
    group_id = event.source.group_id if isinstance(event.source, SourceGroup) else user_id
    user_message = event.message.text.strip()

    if user_message == '@開啟訊息':
        reply_enabled[group_id] = True
        reply = TextSendMessage(text="✅ 已開啟回復訊息。")
        line_bot_api.reply_message(event.reply_token, reply)

    elif user_message == '@關閉訊息':
        reply_enabled[group_id] = False
        reply = TextSendMessage(text="❌ 已關閉回復訊息。")
        line_bot_api.reply_message(event.reply_token, reply)

    elif user_message == "@檢查群組":
        group_name = get_group_name(event)
        reply = TextSendMessage(text=f"📌 這個群組名稱是 `{group_name}`")
        line_bot_api.reply_message(event.reply_token, reply)

    # 【檔案列表查詢】功能
    elif user_message == "@列表":
        group_name = get_group_name(event)
        base_dir = os.path.join("data", group_name)
        categories = {"images": "圖片", "files": "檔案", "videos": "影片"}
        message_lines = ["【上傳檔案列表】"]
        for key, display in categories.items():
            cat_dir = os.path.join(base_dir, key)
            if os.path.exists(cat_dir):
                files = os.listdir(cat_dir)
            else:
                files = []
            message_lines.append(f"\n【{display}】")
            if files:
                for f in files:
                    file_path = os.path.join(cat_dir, f)
                    mtime = os.path.getmtime(file_path)
                    mod_time = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    message_lines.append(f"{f} (上傳時間: {mod_time})")
            else:
                message_lines.append("無檔案")
        final_message = "\n".join(message_lines)
        reply = TextSendMessage(text=final_message)
        line_bot_api.reply_message(event.reply_token, reply)

    # 【檔案刪除】功能，格式：@刪除 <檔案名稱>
    elif user_message.startswith("@刪除"):
        parts = user_message.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            reply = TextSendMessage(text="請提供要刪除的檔案名稱，例如：@刪除 小明-原始檔案名稱.pdf")
            line_bot_api.reply_message(event.reply_token, reply)
            return
        file_to_delete = parts[1].strip()
        group_name = get_group_name(event)
        base_dir = os.path.join("data", group_name)
        categories = ["images", "files", "videos"]
        found = False
        deleted_category = ""
        for category in categories:
            cat_dir = os.path.join(base_dir, category)
            file_path = os.path.join(cat_dir, file_to_delete)
            if os.path.exists(file_path):
                os.remove(file_path)
                found = True
                deleted_category = category
                break
        if found:
            reply_text = f"檔案 `{file_to_delete}` 已從 `{deleted_category}` 刪除。"
        else:
            reply_text = f"找不到檔案 `{file_to_delete}`。"
        reply = TextSendMessage(text=reply_text)
        line_bot_api.reply_message(event.reply_token, reply)

    # 【關鍵字搜尋】功能，格式：@關鍵字 <關鍵字>
    elif user_message.startswith("@關鍵字"):
        parts = user_message.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            reply = TextSendMessage(text="請輸入要搜尋的關鍵字，例如：@關鍵字 test")
            line_bot_api.reply_message(event.reply_token, reply)
            return
        keyword = parts[1].strip()
        group_name = get_group_name(event)
        base_dir = os.path.join("data", group_name)
        categories = {"images": "圖片", "files": "檔案", "videos": "影片"}
        message_lines = [f"【包含關鍵字 '{keyword}' 的檔案列表】"]
        found_any = False
        for key, display in categories.items():
            cat_dir = os.path.join(base_dir, key)
            if os.path.exists(cat_dir):
                files = os.listdir(cat_dir)
            else:
                files = []
            # 使用不區分大小寫搜尋
            matching_files = [f for f in files if keyword.lower() in f.lower()]
            if matching_files:
                found_any = True
                message_lines.append(f"\n【{display}】")
                for f in matching_files:
                    file_path = os.path.join(cat_dir, f)
                    mtime = os.path.getmtime(file_path)
                    mod_time = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    message_lines.append(f"{f} (上傳時間: {mod_time})")
        if not found_any:
            message_lines.append("找不到符合關鍵字的檔案。")
        final_message = "\n".join(message_lines)
        reply = TextSendMessage(text=final_message)
        line_bot_api.reply_message(event.reply_token, reply)

# 處理圖片訊息
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_name = get_user_name(event)
    group_name = get_group_name(event)
    group_folder = os.path.join("data", group_name, "images")
    os.makedirs(group_folder, exist_ok=True)
    image_id = event.message.id
    image_content = line_bot_api.get_message_content(image_id)
    image_filename = f"{user_name}-{image_id}.jpg"
    unique_filename = get_unique_filename(group_folder, image_filename)
    image_path = os.path.join(group_folder, unique_filename)
    with open(image_path, 'wb') as f:
        for chunk in image_content.iter_content():
            f.write(chunk)
    group_identifier = event.source.group_id if isinstance(event.source, SourceGroup) else user_name
    if reply_enabled.get(group_identifier, False):
        reply = TextSendMessage(text=f"📸 圖片已儲存為 `{unique_filename}`！")
        line_bot_api.reply_message(event.reply_token, reply)

# 處理檔案訊息（檔名以 使用者的顯示名稱-原始檔名 命名）
@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    user_name = get_user_name(event)
    group_name = get_group_name(event)
    group_folder = os.path.join("data", group_name, "files")
    os.makedirs(group_folder, exist_ok=True)
    file_id = event.message.id
    original_file_name = event.message.file_name
    file_content = line_bot_api.get_message_content(file_id)
    new_file_name = f"{user_name}-{original_file_name}"
    unique_file_name = get_unique_filename(group_folder, new_file_name)
    file_path = os.path.join(group_folder, unique_file_name)
    with open(file_path, 'wb') as f:
        for chunk in file_content.iter_content():
            f.write(chunk)
    group_identifier = event.source.group_id if isinstance(event.source, SourceGroup) else user_name
    if reply_enabled.get(group_identifier, False):
        reply = TextSendMessage(text=f"📁 檔案已儲存為 `{unique_file_name}`！")
        line_bot_api.reply_message(event.reply_token, reply)

# 處理影片訊息
@handler.add(MessageEvent, message=VideoMessage)
def handle_video_message(event):
    user_name = get_user_name(event)
    group_name = get_group_name(event)
    group_folder = os.path.join("data", group_name, "videos")
    os.makedirs(group_folder, exist_ok=True)
    video_id = event.message.id
    video_content = line_bot_api.get_message_content(video_id)
    video_filename = f"{user_name}-{video_id}.mp4"
    unique_video_filename = get_unique_filename(group_folder, video_filename)
    video_path = os.path.join(group_folder, unique_video_filename)
    with open(video_path, 'wb') as f:
        for chunk in video_content.iter_content():
            f.write(chunk)
    group_identifier = event.source.group_id if isinstance(event.source, SourceGroup) else user_name
    if reply_enabled.get(group_identifier, False):
        reply = TextSendMessage(text=f"🎬 影片已儲存為 `{unique_video_filename}`！")
        line_bot_api.reply_message(event.reply_token, reply)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
