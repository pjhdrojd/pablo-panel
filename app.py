import os
import sys
import uuid
import json
import base64
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pablo-rail-secret-key-change-me")

# =========================================================
# تنظیمات اصلی
# =========================================================
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin")
XRAY_PORT = 10000
FLASK_PORT = 5000
DB_PATH = "users.db"
XRAY_CONFIG_PATH = "xray_config.json"
NGINX_CONFIG_PATH = "nginx.conf"

# =========================================================
# دیتابیس
# =========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            uuid TEXT UNIQUE NOT NULL,
            quota_gb REAL DEFAULT 0,
            used_bytes INTEGER DEFAULT 0,
            expire_days INTEGER DEFAULT 30,
            created_at TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_admin_credentials():
    env_user = os.environ.get("ADMIN_USER")
    env_pass = os.environ.get("ADMIN_PASS")
    if env_user is not None and env_pass is not None:
        return env_user, env_pass
    settings_file = "panel_settings.json"
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("username", "admin"), data.get("password", "admin")
        except Exception:
            pass
    return "admin", "admin"

def save_admin_credentials(username, password):
    data = {"username": username, "password": password}
    with open("panel_settings.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

# =========================================================
# ساخت کانفیگ Xray و Nginx (کاملاً دست‌نخورده برای حفظ پینگ)
# =========================================================
def build_xray_config():
    users = get_all_users()
    clients = [{"id": u["uuid"], "email": u["name"], "level": 0} for u in users if u["enabled"] == 1]
    if not clients:
        clients.append({"id": str(uuid.uuid4()), "email": "default_user", "level": 0})

    config = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService", "HandlerService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True}
        },
        "inbounds": [
            {
                "port": XRAY_PORT, "listen": "127.0.0.1", "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": "/ws"}},
                "tag": "vless-in"
            },
            {"listen": "127.0.0.1", "port": 10001, "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}, "tag": "api"}
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]}
    }
    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def restart_xray():
    build_xray_config()
    try:
        subprocess.run(["pkill", "-9", "-f", "xray"], check=False)
        time.sleep(0.3)
    except: pass
    try:
        subprocess.Popen(["/usr/local/bin/xray/xray", "run", "-c", XRAY_CONFIG_PATH], stdout=sys.stdout, stderr=sys.stderr)
    except: pass

def start_nginx():
    port = os.environ.get("PORT", "8080")
    nginx_conf = f"""
pid /run/nginx.pid;
error_log /dev/stderr warn;
events {{ worker_connections 1024; }}
http {{
    access_log /dev/stdout; include /etc/nginx/mime.types; default_type application/octet-stream; sendfile on; keepalive_timeout 65;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
    server {{
        listen {port}; server_name _;
        location ~ ^/ws {{
            proxy_redirect off; rewrite ^/ws.*$ /ws break; proxy_pass http://127.0.0.1:{XRAY_PORT};
            proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $http_host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{FLASK_PORT};
            proxy_set_header Host $http_host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""
    with open(NGINX_CONFIG_PATH, "w", encoding="utf-8") as f: f.write(nginx_conf)
    try:
        subprocess.run(["pkill", "-9", "-f", "nginx"], check=False)
        time.sleep(0.3)
    except: pass
    subprocess.Popen(["nginx", "-c", os.path.abspath(NGINX_CONFIG_PATH), "-g", "daemon off;"])

def update_stats_loop():
    while True:
        time.sleep(15)
        try:
            res = subprocess.run(["/usr/local/bin/xray/xray", "api", "statsquery", "--server=127.0.0.1:10001"], capture_output=True, text=True, check=False)
            should_restart = False
            conn = get_db(); c = conn.cursor()
            if res.returncode == 0 and res.stdout:
                try:
                    data = json.loads(res.stdout)
                    user_traffic = {}
                    for item in data.get("stat", []):
                        name, value = item.get("name", ""), int(item.get("value", 0) or 0)
                        if "user>>>" in name and "traffic>>>" in name:
                            parts = name.split(">>>")
                            if len(parts) >= 2:
                                email = parts[1]
                                user_traffic[email] = user_traffic.get(email, 0) + value
                    for email, bytes_used in user_traffic.items():
                        c.execute("UPDATE users SET used_bytes=? WHERE name=?", (bytes_used, email))
                except: pass
            
            c.execute("SELECT id, name, quota_gb, used_bytes, expire_days, created_at, enabled FROM users WHERE enabled=1")
            active_users = c.fetchall()
            now = datetime.now()
            for u in active_users:
                try:
                    quota_bytes = float(u["quota_gb"]) * (1024 ** 3)
                    used_bytes = int(u["used_bytes"] or 0)
                    days_passed = (now - datetime.fromisoformat(u["created_at"])).days
                    if ((float(u["quota_gb"]) > 0) and (used_bytes >= quota_bytes)) or (days_passed >= int(u["expire_days"])):
                        c.execute("UPDATE users SET enabled=0 WHERE id=?", (u["id"],))
                        should_restart = True
                except: continue
            conn.commit(); conn.close()
            if should_restart: restart_xray()
        except: pass

def make_all_vless_configs(user, host):
    created_dt = datetime.fromisoformat(user["created_at"])
    days_left = max(0, user["expire_days"] - (datetime.now() - created_dt).days)
    used_gb = round(user["used_bytes"] / (1024 ** 3), 2)
    quota_gb = round(float(user["quota_gb"]), 2)
    remaining_gb = max(0.0, round(quota_gb - used_gb, 2))
    u_uuid, name = user["uuid"], user["name"]
    remark_text = f"{name} | {used_gb:.2f} GB/{quota_gb:.2f} GB (باقی {remaining_gb:.2f} GB) | {days_left}د 0س"
    encoded_remark = urllib.parse.quote(remark_text)
    
    configs = []
    configs.append({"title": "🚀 کانفیگ اصلی (VodiWalker TLS)", "desc": "پایدارترین اتصال برای تمامی اپراتورها", "tag": "VodiWalker TLS", "config": f"vless://{u_uuid}@{host}:443?path=%2Fws%2F{u_uuid}&security=tls&alpn=http%2F1.1&encryption=none&insecure=0&host={host}&fp=chrome&type=ws&allowInsecure=0&sni={host}#{encoded_remark}"})
    configs.append({"title": "⚡ کانفیگ ضد فیلتر (EarlyData)", "desc": "مخصوص همراه اول، ایرانسل و رایتل", "tag": "AntiFilter", "config": f"vless://{u_uuid}@{host}:443?path=%2Fws%2F{u_uuid}%3Fed%3D2560&security=tls&alpn=http%2F1.1&encryption=none&insecure=0&host={host}&fp=chrome&type=ws&allowInsecure=0&sni={host}#{encoded_remark}%20%5BAntiFilter%5D"})
    configs.append({"title": "🛡️ کانفیگ مالتی ALPN (Firefox)", "desc": "مخصوص اینترنت خانگی، مخابرات و وای‌فای", "tag": "Firefox", "config": f"vless://{u_uuid}@{host}:443?path=%2Fws%2F{u_uuid}&security=tls&alpn=h2%2Chttp%2F1.1&encryption=none&insecure=0&host={host}&fp=firefox&type=ws&allowInsecure=0&sni={host}#{encoded_remark}%20%5BFirefox%5D"})
    configs.append({"title": "📱 کانفیگ سافاری (iOS / V2Box)", "desc": "بهینه‌شده برای گوشی‌های آیفون", "tag": "Safari iOS", "config": f"vless://{u_uuid}@{host}:443?path=%2Fws%2F{u_uuid}&security=tls&alpn=http%2F1.1&encryption=none&insecure=0&host={host}&fp=safari&type=ws&allowInsecure=0&sni={host}#{encoded_remark}%20%5BSafari-iOS%5D"})
    configs.append({"title": "🌐 کانفیگ بدون TLS (پورت 80)", "desc": "برای زمان اختلال شدید پروتکل TLS", "tag": "HTTP-80", "config": f"vless://{u_uuid}@{host}:80?path=%2Fws%2F{u_uuid}&security=none&encryption=none&host={host}&type=ws#{encoded_remark}%20%5BHTTP-80%5D"})
    return configs

# =========================================================
# روت‌ها
# =========================================================
@app.route("/")
def home():
    if "admin" not in session: return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        current_username, current_password = get_admin_credentials()
        if username == current_username and password == current_password:
            session["admin"] = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="نام کاربری یا رمز عبور اشتباه است!")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "admin" not in session: return redirect(url_for("login"))
    users = get_all_users()
    return render_template("dashboard.html", 
        total_users=len(users), 
        active_users=sum(1 for u in users if u["enabled"] == 1),
        total_gb=round(sum(u["quota_gb"] for u in users), 2), 
        total_used=round(sum(u["used_bytes"] for u in users) / (1024 ** 3), 2)
    )

# روت جدید: صفحه اختصاصی مدیریت کاربران
@app.route("/users")
def users_page():
    if "admin" not in session: return redirect(url_for("login"))
    users = get_all_users()
    for u in users:
        u['expiry_str'] = (datetime.fromisoformat(u['created_at']) + timedelta(days=u['expire_days'])).strftime('%Y-%m-%d')
    return render_template("users.html", users=users)

@app.route("/settings")
def settings():
    if "admin" not in session: return redirect(url_for("login"))
    username, _ = get_admin_credentials()
    return render_template("settings.html", current_username=username)

@app.route("/api/add_user", methods=["POST"])
def add_user():
    if "admin" not in session: return jsonify({"status": "error"}), 401
    data = request.json or {}
    name = data.get("name", "").strip()
    try: quota, days = float(data.get("quota", 30)), int(data.get("days", 30))
    except: return jsonify({"status": "error", "message": "حجم یا روز نامعتبر"}), 400
    if not name: return jsonify({"status": "error", "message": "نام کاربر الزامی است"}), 400
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO users (name, uuid, quota_gb, expire_days, created_at) VALUES (?, ?, ?, ?, ?)", (name, str(uuid.uuid4()), quota, days, datetime.now().isoformat()))
        conn.commit(); conn.close(); restart_xray()
        return jsonify({"status": "success"})
    except: return jsonify({"status": "error", "message": "نام تکراری است"}), 400

@app.route("/api/delete_user/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if "admin" not in session: return jsonify({"status": "error"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (user_id,)); conn.commit(); conn.close(); restart_xray()
    return jsonify({"status": "success"})

@app.route("/api/toggle_user/<int:user_id>", methods=["POST"])
def toggle_user(user_id):
    if "admin" not in session: return jsonify({"status": "error"}), 401
    conn = get_db(); c = conn.cursor(); c.execute("SELECT enabled FROM users WHERE id=?", (user_id,))
    row = c.fetchone()
    if row:
        new_val = 0 if row[0] == 1 else 1
        c.execute("UPDATE users SET enabled=? WHERE id=?", (new_val, user_id)); conn.commit(); conn.close(); restart_xray()
        return jsonify({"status": "success", "new_state": new_val})
    return jsonify({"status": "error"}), 404

# روت جدید: ریست کردن حجم کاربر
@app.route("/api/reset_user/<int:user_id>", methods=["POST"])
def reset_user(user_id):
    if "admin" not in session: return jsonify({"status": "error"}), 401
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE users SET used_bytes=0, enabled=1 WHERE id=?", (user_id,))
    conn.commit(); conn.close(); restart_xray()
    return jsonify({"status": "success"})

@app.route("/api/user_config/<int:user_id>")
def user_config(user_id):
    if "admin" not in session: return jsonify({"status": "error"}), 401
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users WHERE id=?", (user_id,))
    user = c.fetchone(); conn.close()
    if not user: return jsonify({"status": "error"}), 404
    host = request.host.split(":")[0]
    return jsonify({"status": "success", "configs": make_all_vless_configs(dict(user), host), "sub": f"{request.host_url}sub/{user['uuid']}", "user": dict(user)})

@app.route("/sub/<user_uuid>")
def subscription(user_uuid):
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users WHERE uuid=?", (user_uuid,))
    user = c.fetchone(); conn.close()
    if not user or user["enabled"] == 0: return "User not found or disabled", 404
    ua = request.headers.get("User-Agent", "").lower()
    is_client = any(k in ua for k in ["v2ray", "clash", "sing-box", "hiddify", "nekobox", "streisand", "foxray", "shadowrocket", "v2box"])
    host = request.host.split(":")[0]
    all_configs = make_all_vless_configs(dict(user), host)
    
    if is_client:
        return Response(base64.b64encode("\n".join(c["config"] for c in all_configs).encode()).decode(), mimetype="text/plain")

    user_dict = dict(user)
    created_dt = datetime.fromisoformat(user_dict["created_at"])
    days_left = max(0, user_dict["expire_days"] - (datetime.now() - created_dt).days)
    used_gb = round(user_dict["used_bytes"] / (1024 ** 3), 2)
    percent = (used_gb / user_dict["quota_gb"] * 100) if user_dict["quota_gb"] > 0 else 0
    encoded_sub = base64.b64encode("\n".join(c["config"] for c in all_configs).encode()).decode()

    return render_template("subscription.html", user_name=user_dict["name"], used_gb=used_gb, quota_gb=user_dict["quota_gb"],
                           remaining_gb=max(0.0, round(user_dict["quota_gb"] - used_gb, 2)), days_left=days_left, percent=round(percent, 1),
                           configs=all_configs, sub_raw=encoded_sub, sub_url=request.url)

if __name__ == "__main__":
    init_db()
    restart_xray()
    start_nginx()
    threading.Thread(target=update_stats_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=FLASK_PORT)
