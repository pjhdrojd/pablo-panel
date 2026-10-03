import os
import sys
import uuid
import json
import base64
import sqlite3
import subprocess
import time
import urllib.parse
from datetime import datetime
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


# =========================================================
# مدیریت تنظیمات ورود پنل
# =========================================================

def get_admin_credentials():
    """
    اگر ADMIN_USER و ADMIN_PASS در Environment تعریف شده باشند،
    همان‌ها اولویت دارند.

    در غیر این صورت از فایل panel_settings.json استفاده می‌شود.
    """

    env_user = os.environ.get("ADMIN_USER")
    env_pass = os.environ.get("ADMIN_PASS")

    if env_user is not None and env_pass is not None:
        return env_user, env_pass

    settings_file = "panel_settings.json"

    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            username = data.get("username", "admin")
            password = data.get("password", "admin")

            return username, password

        except Exception:
            pass

    return "admin", "admin"


def save_admin_credentials(username, password):
    """
    ذخیره نام کاربری و رمز جدید.
    """

    settings_file = "panel_settings.json"

    data = {
        "username": username,
        "password": password
    }

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# =========================================================
# کاربران
# =========================================================

def get_all_users():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users ORDER BY id DESC")

    rows = [dict(r) for r in c.fetchall()]

    conn.close()

    return rows


# =========================================================
# ساخت کانفیگ Xray
# =========================================================

def build_xray_config():

    users = get_all_users()

    clients = []

    for u in users:

        if u["enabled"] == 1:

            clients.append({
                "id": u["uuid"],
                "email": u["name"],
                "level": 0
            })

    if not clients:

        clients.append({
            "id": str(uuid.uuid4()),
            "email": "default_user",
            "level": 0
        })

    config = {
        "log": {
            "loglevel": "warning"
        },

        "inbounds": [
            {
                "port": XRAY_PORT,
                "listen": "127.0.0.1",
                "protocol": "vless",

                "settings": {
                    "clients": clients,
                    "decryption": "none"
                },

                "streamSettings": {
                    "network": "ws",
                    "security": "none",

                    "wsSettings": {
                        "path": "/ws"
                    }
                }
            }
        ],

        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "direct"
            }
        ]
    }

    with open(
        XRAY_CONFIG_PATH,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            config,
            f,
            indent=2,
            ensure_ascii=False
        )


# =========================================================
# ری‌استارت Xray
# =========================================================

def restart_xray():

    build_xray_config()

    try:

        subprocess.run(
            ["pkill", "-9", "-f", "xray"],
            check=False
        )

        time.sleep(0.3)

    except Exception:

        pass

    try:

        subprocess.Popen(
            [
                "/usr/local/bin/xray/xray",
                "run",
                "-c",
                XRAY_CONFIG_PATH
            ],
            stdout=sys.stdout,
            stderr=sys.stderr
        )

    except Exception as e:

        print("Xray start error:", e)


# =========================================================
# NGINX
# =========================================================

def start_nginx():

    port = os.environ.get(
        "PORT",
        "8080"
    )

    nginx_conf = f"""
pid /run/nginx.pid;

error_log /dev/stderr warn;

events {{
    worker_connections 1024;
}}

http {{

    access_log /dev/stdout;

    include /etc/nginx/mime.types;

    default_type application/octet-stream;

    sendfile on;

    keepalive_timeout 65;

    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}

    server {{

        listen {port};

        server_name _;


        # ================================
        # WebSocket / Xray
        # ================================

        location ~ ^/ws {{

            proxy_redirect off;

            rewrite ^/ws.*$ /ws break;

            proxy_pass http://127.0.0.1:{XRAY_PORT};

            proxy_http_version 1.1;

            proxy_set_header Upgrade $http_upgrade;

            proxy_set_header Connection $connection_upgrade;

            proxy_set_header Host $http_host;

            proxy_set_header X-Real-IP $remote_addr;

            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

            proxy_read_timeout 86400s;

            proxy_send_timeout 86400s;
        }}


        # ================================
        # Flask Panel
        # ================================

        location / {{

            proxy_pass http://127.0.0.1:{FLASK_PORT};

            proxy_set_header Host $http_host;

            proxy_set_header X-Real-IP $remote_addr;

            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}
}}
"""

    with open(
        NGINX_CONFIG_PATH,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(nginx_conf)

    try:

        subprocess.run(
            ["pkill", "-9", "-f", "nginx"],
            check=False
        )

        time.sleep(0.3)

    except Exception:

        pass

    subprocess.Popen(
        [
            "nginx",
            "-c",
            os.path.abspath(NGINX_CONFIG_PATH),
            "-g",
            "daemon off;"
        ]
    )


# =========================================================
# ساخت کانفیگ‌های VLESS
# =========================================================

def make_all_vless_configs(user, host):

    created_dt = datetime.fromisoformat(
        user["created_at"]
    )

    elapsed_days = (
        datetime.now() - created_dt
    ).days

    days_left = max(
        0,
        user["expire_days"] - elapsed_days
    )

    used_gb = round(
        user["used_bytes"] / (1024 ** 3),
        2
    )

    quota_gb = round(
        float(user["quota_gb"]),
        2
    )

    remaining_gb = max(
        0.0,
        round(quota_gb - used_gb, 2)
    )

    u_uuid = user["uuid"]

    name = user["name"]

    remark_text = (
        f"{name} | "
        f"{used_gb:.2f} GB/"
        f"{quota_gb:.2f} GB "
        f"(باقی {remaining_gb:.2f} GB) | "
        f"{days_left}د 0س"
    )

    encoded_remark = urllib.parse.quote(
        remark_text
    )

    configs = []

    # 1
    c1 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{encoded_remark}"
    )

    configs.append({
        "title": "🚀 کانفیگ اصلی (VodiWalker TLS)",
        "desc": "پایدارترین اتصال برای تمامی اپراتورها",
        "tag": "VodiWalker TLS",
        "config": c1
    })

    # 2
    c2 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}%3Fed%3D2560"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=chrome"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{encoded_remark}%20%5BAntiFilter%5D"
    )

    configs.append({
        "title": "⚡ کانفیگ ضد فیلتر (EarlyData)",
        "desc": "مخصوص همراه اول، ایرانسل و رایتل",
        "tag": "AntiFilter",
        "config": c2
    })

    # 3
    c3 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=h2%2Chttp%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=firefox"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{encoded_remark}%20%5BFirefox%5D"
    )

    configs.append({
        "title": "🛡️ کانفیگ مالتی ALPN (Firefox)",
        "desc": "مخصوص اینترنت خانگی، مخابرات و وای‌فای",
        "tag": "Firefox",
        "config": c3
    })

    # 4
    c4 = (
        f"vless://{u_uuid}@{host}:443"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=tls"
        f"&alpn=http%2F1.1"
        f"&encryption=none"
        f"&insecure=0"
        f"&host={host}"
        f"&fp=safari"
        f"&type=ws"
        f"&allowInsecure=0"
        f"&sni={host}"
        f"#{encoded_remark}%20%5BSafari-iOS%5D"
    )

    configs.append({
        "title": "📱 کانفیگ سافاری (iOS / V2Box)",
        "desc": "بهینه‌شده برای گوشی‌های آیفون",
        "tag": "Safari iOS",
        "config": c4
    })

    # 5
    c5 = (
        f"vless://{u_uuid}@{host}:80"
        f"?path=%2Fws%2F{u_uuid}"
        f"&security=none"
        f"&encryption=none"
        f"&host={host}"
        f"&type=ws"
        f"#{encoded_remark}%20%5BHTTP-80%5D"
    )

    configs.append({
        "title": "🌐 کانفیگ بدون TLS (پورت 80)",
        "desc": "برای زمان اختلال شدید پروتکل TLS",
        "tag": "HTTP-80",
        "config": c5
    })

    return configs


# =========================================================
# روت اصلی
# =========================================================

@app.route("/")
def home():

    if "admin" not in session:

        return redirect(
            url_for("login")
        )

    return redirect(
        url_for("dashboard")
    )


# =========================================================
# Login
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        current_username, current_password = (
            get_admin_credentials()
        )

        if (
            username == current_username
            and password == current_password
        ):

            session["admin"] = True

            return redirect(
                url_for("dashboard")
            )

        return render_template(
            "login.html",
            error="نام کاربری یا رمز عبور اشتباه است!"
        )

    return render_template(
        "login.html",
        error=None
    )


# =========================================================
# Logout
# =========================================================

@app.route("/logout")
def logout():

    session.pop(
        "admin",
        None
    )

    return redirect(
        url_for("login")
    )


# =========================================================
# Dashboard
# =========================================================

@app.route("/dashboard")
def dashboard():

    if "admin" not in session:

        return redirect(
            url_for("login")
        )

    users = get_all_users()

    total_gb = sum(
        u["quota_gb"]
        for u in users
    )

    total_used = sum(
        u["used_bytes"]
        for u in users
    ) / (1024 ** 3)

    active_count = sum(
        1
        for u in users
        if u["enabled"] == 1
    )

    return render_template(
        "dashboard.html",

        users=users,

        total_users=len(users),

        active_users=active_count,

        total_gb=round(
            total_gb,
            2
        ),

        total_used=round(
            total_used,
            2
        )
    )


# =========================================================
# Users Management page
# =========================================================

@app.route("/users")
def users_page():

    if "admin" not in session:

        return redirect(
            url_for("login")
        )

    users = get_all_users()

    total_gb = sum(
        u["quota_gb"]
        for u in users
    )

    total_used = sum(
        u["used_bytes"]
        for u in users
    ) / (1024 ** 3)

    active_count = sum(
        1
        for u in users
        if u["enabled"] == 1
    )

    return render_template(
        "users.html",

        users=users,

        total_users=len(users),

        active_users=active_count,

        total_gb=round(
            total_gb,
            2
        ),

        total_used=round(
            total_used,
            2
        )
    )


# =========================================================
# Settings page
# =========================================================

@app.route("/settings")
def settings():

    if "admin" not in session:

        return redirect(
            url_for("login")
        )

    username, _ = get_admin_credentials()

    return render_template(
        "settings.html",
        current_username=username
    )


# =========================================================
# API تغییر Username / Password
# =========================================================

@app.route(
    "/api/settings",
    methods=["POST"]
)
def update_settings():

    if "admin" not in session:

        return jsonify({
            "status": "error",
            "message": "دسترسی غیرمجاز"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    new_username = data.get(
        "username",
        ""
    ).strip()

    new_password = data.get(
        "password",
        ""
    )

    current_password = data.get(
        "current_password",
        ""
    )

    if not new_username:

        return jsonify({
            "status": "error",
            "message": "نام کاربری جدید الزامی است"
        }), 400

    if not new_password:

        return jsonify({
            "status": "error",
            "message": "رمز عبور جدید الزامی است"
        }), 400

    username, password = (
        get_admin_credentials()
    )

    if current_password != password:

        return jsonify({
            "status": "error",
            "message": "رمز عبور فعلی اشتباه است"
        }), 400

    if len(new_username) < 3:

        return jsonify({
            "status": "error",
            "message": "نام کاربری حداقل باید ۳ کاراکتر باشد"
        }), 400

    if len(new_password) < 4:

        return jsonify({
            "status": "error",
            "message": "رمز عبور حداقل باید ۴ کاراکتر باشد"
        }), 400

    try:

        save_admin_credentials(
            new_username,
            new_password
        )

        session.pop(
            "admin",
            None
        )

        return jsonify({
            "status": "success",
            "message": "اطلاعات ورود با موفقیت تغییر کرد"
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# =========================================================
# Add User
# =========================================================

@app.route(
    "/api/add_user",
    methods=["POST"]
)
def add_user():

    if "admin" not in session:

        return jsonify({
            "status": "error",
            "message": "دسترسی غیرمجاز"
        }), 401

    data = request.json or {}

    name = data.get(
        "name",
        ""
    ).strip()

    try:

        quota = float(
            data.get(
                "quota",
                30
            )
        )

        days = int(
            data.get(
                "days",
                30
            )
        )

    except Exception:

        return jsonify({
            "status": "error",
            "message": "حجم یا تعداد روز نامعتبر است"
        }), 400

    if not name:

        return jsonify({
            "status": "error",
            "message": "نام کاربر الزامی است"
        }), 400

    if quota <= 0:

        return jsonify({
            "status": "error",
            "message": "حجم باید بیشتر از صفر باشد"
        }), 400

    if days <= 0:

        return jsonify({
            "status": "error",
            "message": "تعداد روز باید بیشتر از صفر باشد"
        }), 400

    user_uuid = str(
        uuid.uuid4()
    )

    try:

        conn = get_db()

        c = conn.cursor()

        c.execute(
            """
            INSERT INTO users
            (
                name,
                uuid,
                quota_gb,
                expire_days,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                user_uuid,
                quota,
                days,
                datetime.now().isoformat()
            )
        )

        conn.commit()

        conn.close()

        restart_xray()

        return jsonify({
            "status": "success",
            "message": "کاربر با موفقیت ساخته شد"
        })

    except sqlite3.IntegrityError:

        return jsonify({
            "status": "error",
            "message": "این نام کاربری قبلاً وجود دارد"
        }), 400

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# =========================================================
# Delete User
# =========================================================

@app.route(
    "/api/delete_user/<int:user_id>",
    methods=["POST"]
)
def delete_user(user_id):

    if "admin" not in session:

        return jsonify({
            "status": "error"
        }), 401

    conn = get_db()

    c = conn.cursor()

    c.execute(
        "DELETE FROM users WHERE id=?",
        (user_id,)
    )

    conn.commit()

    conn.close()

    restart_xray()

    return jsonify({
        "status": "success",
        "message": "کاربر با موفقیت حذف شد"
    })


# =========================================================
# Toggle User
# =========================================================

@app.route(
    "/api/toggle_user/<int:user_id>",
    methods=["POST"]
)
def toggle_user(user_id):

    if "admin" not in session:

        return jsonify({
            "status": "error"
        }), 401

    conn = get_db()

    c = conn.cursor()

    c.execute(
        "SELECT enabled FROM users WHERE id=?",
        (user_id,)
    )

    row = c.fetchone()

    if not row:

        conn.close()

        return jsonify({
            "status": "error",
            "message": "کاربر یافت نشد"
        }), 404

    new_val = (
        0
        if row[0] == 1
        else 1
    )

    c.execute(
        "UPDATE users SET enabled=? WHERE id=?",
        (
            new_val,
            user_id
        )
    )

    conn.commit()

    conn.close()

    restart_xray()

    return jsonify({
        "status": "success",
        "new_state": new_val
    })


# =========================================================
# User Config
# =========================================================

@app.route(
    "/api/user_config/<int:user_id>"
)
def user_config(user_id):

    if "admin" not in session:

        return jsonify({
            "status": "error"
        }), 401

    conn = get_db()

    c = conn.cursor()

    c.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,)
    )

    user = c.fetchone()

    conn.close()

    if not user:

        return jsonify({
            "status": "error",
            "message": "کاربر یافت نشد"
        }), 404

    host = request.host.split(":")[0]

    configs = make_all_vless_configs(
        dict(user),
        host
    )

    sub_link = (
        f"{request.host_url}"
        f"sub/{user['uuid']}"
    )

    return jsonify({
        "status": "success",
        "configs": configs,
        "sub": sub_link,
        "user": dict(user)
    })


# =========================================================
# Subscription
# =========================================================

@app.route(
    "/sub/<user_uuid>"
)
def subscription(user_uuid):

    conn = get_db()

    c = conn.cursor()

    c.execute(
        "SELECT * FROM users WHERE uuid=?",
        (user_uuid,)
    )

    user = c.fetchone()

    conn.close()

    if (
        not user
        or user["enabled"] == 0
    ):

        return (
            "User not found or disabled",
            404
        )

    ua = request.headers.get(
        "User-Agent",
        ""
    ).lower()

    client_keywords = [
        "v2ray",
        "clash",
        "sing-box",
        "hiddify",
        "nekobox",
        "streisand",
        "foxray",
        "shadowrocket",
        "v2box"
    ]

    is_client = any(
        k in ua
        for k in client_keywords
    )

    host = request.host.split(":")[0]

    user_dict = dict(user)

    all_configs = make_all_vless_configs(
        user_dict,
        host
    )

    if is_client:

        raw_text = "\n".join(
            item["config"]
            for item in all_configs
        )

        encoded = base64.b64encode(
            raw_text.encode()
        ).decode()

        return Response(
            encoded,
            mimetype="text/plain"
        )

    created_dt = datetime.fromisoformat(
        user_dict["created_at"]
    )

    elapsed_days = (
        datetime.now() - created_dt
    ).days

    days_left = max(
        0,
        user_dict["expire_days"]
        - elapsed_days
    )

    used_gb = round(
        user_dict["used_bytes"]
        / (1024 ** 3),
        2
    )

    remaining_gb = max(
        0.0,
        round(
            user_dict["quota_gb"]
            - used_gb,
            2
        )
    )

    percent = (
        used_gb
        / user_dict["quota_gb"]
        * 100
        if user_dict["quota_gb"] > 0
        else 0
    )

    raw_text = "\n".join(
        item["config"]
        for item in all_configs
    )

    encoded_sub = base64.b64encode(
        raw_text.encode()
    ).decode()

    return render_template(
        "subscription.html",

        user_name=user_dict["name"],

        used_gb=used_gb,

        quota_gb=user_dict["quota_gb"],

        remaining_gb=remaining_gb,

        days_left=days_left,

        percent=round(
            percent,
            1
        ),

        configs=all_configs,

        sub_raw=encoded_sub,

        sub_url=request.url
    )


# =========================================================
# شروع برنامه
# =========================================================

if __name__ == "__main__":

    init_db()

    restart_xray()

    start_nginx()

    app.run(
        host="127.0.0.1",
        port=FLASK_PORT
    )
