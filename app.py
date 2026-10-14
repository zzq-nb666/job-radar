import hashlib
import json
import os
import sqlite3
import time
import uuid

from flask import Flask, request, jsonify, g, send_from_directory, make_response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data.db")

# ---- 岗位库（campus）配置：数据 + 密钥沿用原校招看板口径 ----
CAMPUS_HTML = os.path.join(BASE, "static", "campus.html")
CAMPUS_DATA = os.path.join(BASE, "campus_data.json")
CAMPUS_SECRET = os.path.join(BASE, "campus_secret.json")

app = Flask(__name__, static_folder="static", static_url_path="")


def _campus_key():
    try:
        with open(CAMPUS_SECRET, encoding="utf-8") as f:
            return str(json.load(f).get("accessKey") or "")
    except Exception:
        return ""


def _campus_token():
    return hashlib.sha256(("crd::session::" + _campus_key()).encode()).hexdigest()


def campus_authed():
    return request.cookies.get("crd_session") == _campus_token()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles(
            user_key TEXT PRIMARY KEY,
            name TEXT,
            profile TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS records(
            id TEXT PRIMARY KEY,
            user_key TEXT NOT NULL,
            company TEXT NOT NULL,
            role TEXT,
            city TEXT,
            channel TEXT,
            date TEXT,
            status TEXT,
            next TEXT,
            note TEXT,
            link TEXT,
            remind_date TEXT,
            created_at TEXT
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(records)").fetchall()]
    if "user_key" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN user_key TEXT")
    if "remind_date" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN remind_date TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON records(user_key)")
    conn.commit()
    conn.close()


def load_profile(uk):
    if not uk:
        return None
    row = get_db().execute(
        "SELECT profile FROM user_profiles WHERE user_key=?", (uk,)
    ).fetchone()
    if not row or not row["profile"]:
        return None
    try:
        return json.loads(row["profile"])
    except Exception:
        return None


def save_profile(uk, name, prof):
    get_db().execute(
        "INSERT INTO user_profiles(user_key,name,profile,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_key) DO UPDATE SET name=excluded.name, profile=excluded.profile",
        (uk, name, json.dumps(prof, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    get_db().commit()


# ---------------- 个性化匹配引擎：按每个用户的求职意向重算匹配度 ----------------

DIR_ALL = ["产品类", "项目助理类", "运营类", "数据类", "采购供应链类",
           "技术研发类", "设计类", "市场类", "销售类", "职能类", "制造工艺类", "管培生"]

DIR_KW = {
    "产品类": ["产品"],
    "项目助理类": ["项目", "PMO", "交付", "实施"],
    "运营类": ["运营"],
    "数据类": ["数据分析", "商业分析", "业务分析", "数据"],
    "采购供应链类": ["采购", "供应链", "寻源", "物资", "sourcing", "procurement"],
    "技术研发类": ["算法", "开发", "测试", "运维", "嵌入式", "前端", "后端",
                 "Java", "C++", "架构", "芯片", "软件", "人工智能", "AI",
                 "机器学习", "研究员", "硬件"],
    "设计类": ["设计", "UI", "UX", "视觉", "交互"],
    "市场类": ["市场", "品牌", "公关", "渠道"],
    "销售类": ["销售", "售前", "客户经理", "商务"],
    "职能类": ["人力", "HR", "财务", "会计", "法务", "行政", "审计", "税务"],
    "制造工艺类": ["工艺", "机械", "电气", "质量", "设备", "生产", "制造"],
    "管培生": ["管培", "管理培训生", "储备干部", "储备生", "菁英"],
}

MAJOR_IND = {
    "自动化": ["机械", "电气", "仪器", "工业", "智造", "汽车", "能源", "机器人",
             "半导体", "电力", "通信", "装备", "制造", "电子"],
    "计算机": ["软件", "互联网", "信息技术", "数据", "云计算", "人工智能", "电子"],
    "机械": ["机械", "装备", "制造", "汽车", "工业", "机器人", "能源"],
    "电子": ["电子", "半导体", "集成电路", "通信", "硬件"],
}

_MAJOR_STOP = {"专业", "工程", "技术", "科学", "及其", "与", "类", "及", "学", "院", "大学"}


def _major_hit(major, ind_roles):
    """专业自由文本 → 是否与岗位行业/岗位名相关。"""
    if not major:
        return False
    # 1) 命中预定义专业映射
    for k, kws in MAJOR_IND.items():
        if k in major and any(x in ind_roles for x in kws):
            return True
    # 2) 拆词兜底：把专业名拆成 2~4 字词，去掉停用词，看是否出现在岗位文本里
    import re as _re
    toks = [t for t in _re.findall(r"[\u4e00-\u9fff]{2,4}", major) if t not in _MAJOR_STOP]
    return any(t in ind_roles for t in toks)


def compute_fit(rec, p):
    """按用户求职意向重算单条岗位的匹配分与理由。"""
    cats = rec.get("categories") or []
    roles = " ".join(rec.get("roles") or [])
    ind = rec.get("industry") or ""
    dirs = p.get("directions") or []

    fit = 55
    hit_dirs = [d for d in dirs if d in cats or any(k in roles for k in DIR_KW.get(d, []))]
    if dirs and hit_dirs:
        fit += 22
    cities = p.get("cities") or []
    if not cities:
        fit += 8
        city_txt = "不限城市"
    else:
        inter = [c for c in (rec.get("cities") or []) if c in cities]
        if inter:
            fit += 12
            city_txt = "、".join(inter[:3])
        else:
            fit -= 10
            city_txt = "城市不符"
    ind_hit = _major_hit(p.get("major", ""), ind + roles)
    if ind_hit:
        fit += 8
    custom = p.get("custom_dirs") or []
    if custom and any(k in (roles + ind) for k in custom):
        fit += 6
    warn = ""
    if rec.get("has_test") and not p.get("accept_test", True):
        fit -= 18
        warn = " · ⚠流程含笔试"
    fit = max(40, min(95, fit))

    reason = ("🎯 按你的意向：方向 %s · 城市 %s · 专业相关行业 %s%s。原备注：%s" % (
        "、".join(hit_dirs[:2]) if hit_dirs else "未命中",
        city_txt,
        "✓" if ind_hit else "—",
        warn,
        (rec.get("fitReason") or "")[:70],
    ))
    rec["fit"] = fit
    rec["fitReason"] = reason
    return rec


def row2dict(r):
    return {k: r[k] for k in r.keys()}


def get_user_key():
    uk = request.args.get("user_key")
    if not uk:
        body = request.get_json(silent=True) or {}
        uk = body.get("user_key")
    return uk


@app.route("/api/stats", methods=["GET"])
def stats():
    row = get_db().execute(
        "SELECT COUNT(DISTINCT user_key) AS users, COUNT(*) AS records FROM records"
    ).fetchone()
    return jsonify({"users": row["users"], "records": row["records"]})


@app.route("/api/records", methods=["GET"])
def list_records():
    uk = get_user_key()
    if not uk:
        return jsonify({"error": "user_key required"}), 401
    rows = get_db().execute(
        "SELECT * FROM records WHERE user_key=? ORDER BY date DESC, created_at DESC", (uk,)
    ).fetchall()
    return jsonify([row2dict(r) for r in rows])


@app.route("/api/records", methods=["POST"])
def create_record():
    d = request.get_json(force=True) or {}
    uk = d.get("user_key")
    if not uk:
        return jsonify({"error": "user_key required"}), 401
    if not d.get("company"):
        return jsonify({"error": "company required"}), 400
    rid = uuid.uuid4().hex[:10]
    get_db().execute(
        "INSERT INTO records(id,user_key,company,role,city,channel,date,status,next,note,link,remind_date,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid,
            uk,
            d.get("company", ""),
            d.get("role", ""),
            d.get("city", ""),
            d.get("channel", ""),
            d.get("date", ""),
            d.get("status", "已投递"),
            d.get("next", ""),
            d.get("note", ""),
            d.get("link", ""),
            d.get("remind_date", ""),
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    get_db().commit()
    return jsonify({"id": rid}), 201


@app.route("/api/records/<rid>", methods=["PUT"])
def update_record(rid):
    d = request.get_json(force=True) or {}
    uk = d.get("user_key")
    cur = get_db().execute("SELECT user_key FROM records WHERE id=?", (rid,)).fetchone()
    if not cur:
        return jsonify({"error": "not found"}), 404
    if cur["user_key"] != uk:
        return jsonify({"error": "forbidden"}), 403
    get_db().execute(
        "UPDATE records SET company=?,role=?,city=?,channel=?,date=?,status=?,next=?,note=?,link=?,remind_date=? WHERE id=?",
        (
            d.get("company", ""),
            d.get("role", ""),
            d.get("city", ""),
            d.get("channel", ""),
            d.get("date", ""),
            d.get("status", "已投递"),
            d.get("next", ""),
            d.get("note", ""),
            d.get("link", ""),
            d.get("remind_date", ""),
            rid,
        ),
    )
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/api/records/<rid>", methods=["DELETE"])
def delete_record(rid):
    uk = get_user_key()
    cur = get_db().execute("SELECT user_key FROM records WHERE id=?", (rid,)).fetchone()
    if not cur:
        return jsonify({"error": "not found"}), 404
    if cur["user_key"] != uk:
        return jsonify({"error": "forbidden"}), 403
    get_db().execute("DELETE FROM records WHERE id=?", (rid,))
    get_db().commit()
    return jsonify({"ok": True})


@app.route("/")
def index():
    # 首页 = 岗位库看板（统一昵称+口令登录 + 按个人求职意向重算匹配）
    name = request.cookies.get("radar_name")
    uk = request.cookies.get("radar_uk")
    if not name or not uk:
        return make_response(_LOGIN_GATE.replace("__ERR__", ""), 200)
    prof = load_profile(uk)
    if not prof:
        resp = make_response("", 302)
        resp.headers["Location"] = "/setup"
        return resp
    return _campus_page(name, prof)


_SETUP_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>上岸雷达 · 求职意向</title>
<style>body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f7;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#1c2430;padding:20px 0;}
.card{background:#fff;border:1px solid #e6ecf4;border-radius:14px;padding:26px 30px;width:430px;box-shadow:0 1px 3px rgba(16,24,40,.06);}
h1{font-size:18px;margin:0 0 4px;}p.sub{font-size:12.5px;color:#7a879a;margin:0 0 14px;}
fieldset{border:1px solid #e6ecf4;border-radius:10px;padding:10px 14px 12px;margin:0 0 12px;}
legend{font-size:13px;font-weight:600;color:#374151;padding:0 6px;}
label.chk{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:#24344a;margin:5px 12px 5px 0;cursor:pointer;font-weight:400;}
select{width:100%;box-sizing:border-box;font-size:14px;padding:9px 10px;border:1px solid #d3dbe6;border-radius:8px;background:#fbfcfe;}
button{width:100%;margin-top:8px;font-size:14.5px;font-weight:600;color:#fff;background:#1e4b8f;border:0;border-radius:9px;padding:11px;cursor:pointer;}</style></head>
<body><div class="card"><h1>设置我的求职意向</h1><p class="sub">填完这一分钟，岗位马上按你的条件重新计算匹配度</p>
<form method="post" action="/setup">
<fieldset><legend>你的专业</legend>
<input name="major" value="__MAJOR_VAL__" placeholder="自由填写，如：自动化 / 计算机科学与技术"></fieldset>
<fieldset><legend>想要的方向（可多选，也可自填关键词）</legend>__DIR_CHECKS__
<input name="custom_dirs" value="__CUSTOM_VAL__" placeholder="自定义关键词，逗号分隔，如：AI产品、智能制造、供应链" style="margin-top:8px;"></fieldset>
<fieldset><legend>能去的城市（逗号分隔，留空=不限）</legend>
<input name="cities_text" value="__CITY_VAL__" placeholder="如：北京、上海、杭州；留空表示不限城市"></fieldset>
<fieldset><legend>笔试</legend>
<label class="chk"><input type="radio" name="accept_test" value="1" __AT_YES__>可以接受笔试</label>
<label class="chk"><input type="radio" name="accept_test" value="0" __AT_NO__>只想投免笔试的（含笔试的自动降权）</label></fieldset>
<button type="submit">保存，进入我的岗位库 →</button></form></div></body></html>"""


def _split_text(s):
    import re as _re
    return [x for x in _re.split(r"[,，、;；\s]+", (s or "").strip()) if x]


def _setup_page(prof):
    prof = prof or {}
    dirs = prof.get("directions") or []
    cities = prof.get("cities") or []
    major = prof.get("major") or ""
    custom = prof.get("custom_dirs") or []
    at = prof.get("accept_test", False)
    dc = "".join('<label class="chk"><input type="checkbox" name="directions" value="%s"%s>%s</label>'
                 % (d, " checked" if d in dirs else "", d) for d in DIR_ALL)
    html = (_SETUP_PAGE.replace("__MAJOR_VAL__", major)
            .replace("__DIR_CHECKS__", dc)
            .replace("__CUSTOM_VAL__", "、".join(custom))
            .replace("__CITY_VAL__", "、".join(cities))
            .replace("__AT_YES__", " checked" if at else "")
            .replace("__AT_NO__", "" if at else " checked"))
    return html


@app.route("/setup", methods=["GET", "POST"])
def setup():
    name = request.cookies.get("radar_name")
    uk = request.cookies.get("radar_uk")
    if not name or not uk:
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        return resp
    if request.method == "POST":
        prof = {
            "major": (request.form.get("major") or "").strip(),
            "directions": request.form.getlist("directions"),
            "cities": _split_text(request.form.get("cities_text")),
            "custom_dirs": _split_text(request.form.get("custom_dirs")),
            "accept_test": request.form.get("accept_test") == "1",
        }
        if not prof["directions"]:
            prof["directions"] = DIR_ALL[:]
        save_profile(uk, name, prof)
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        return resp
    return _setup_page(load_profile(uk))


def _campus_page(name, prof):
    try:
        with open(CAMPUS_DATA, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        payload = {"meta": {}, "progressOptions": [], "records": []}
    # 核心：按登录者自己的求职意向重算每条岗位的匹配分
    for rec in payload.get("records") or []:
        compute_fit(rec, prof)
    payload["records"].sort(key=lambda r: -r.get("fit", 0))
    with open(CAMPUS_HTML, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__PROFILE__", json.dumps(prof, ensure_ascii=False))
    return html.replace("__USER__", json.dumps(name, ensure_ascii=False))


# ---------------- 统一登录（昵称+口令，即注册即登录） ----------------

_LOGIN_GATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>上岸雷达 · 登录</title>
<style>body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f7;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#1c2430;}
.card{background:#fff;border:1px solid #e6ecf4;border-radius:14px;padding:30px 34px;width:360px;box-shadow:0 1px 3px rgba(16,24,40,.06);}
h1{font-size:19px;margin:0 0 6px;}p.sub{font-size:12.5px;color:#7a879a;margin:0 0 16px;}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 5px;color:#374151;}
input{width:100%;box-sizing:border-box;font-size:14px;padding:10px 12px;border:1px solid #d3dbe6;border-radius:8px;outline:none;}
button{width:100%;margin-top:18px;font-size:14.5px;font-weight:600;color:#fff;background:#1e4b8f;border:0;border-radius:9px;padding:11px;cursor:pointer;}
.err{color:#c0392b;font-size:12px;margin-top:10px;min-height:16px;}
.tip{font-size:12px;color:#8d99aa;margin-top:14px;line-height:1.7;border-top:1px dashed #e6ecf4;padding-top:12px;}</style></head>
<body><div class="card"><h1>上岸雷达 📡</h1><p class="sub">岗位库 + 投递记录，一个口令全管</p>
<form method="post" action="/login">
<label>你的昵称</label><input name="name" placeholder="如：小左" autofocus>
<label>口令</label><input type="password" name="pass" placeholder="首次输入即注册；下次输入同样组合即可找回数据">
<button type="submit">进入</button><div class="err">__ERR__</div></form>
<div class="tip">进入后：岗位库每日自动更新，投递进度、提醒、转化漏斗全部归你个人。换设备用「同样的昵称+口令」登录即可找回。</div></div></body></html>"""


@app.route("/login", methods=["POST"])
def login():
    name = (request.form.get("name") or "").strip()
    pwd = (request.form.get("pass") or "").strip()
    if not name or not pwd:
        return make_response(_LOGIN_GATE.replace("__ERR__", "昵称和口令都要填"), 200)
    uk = hashlib.sha256((name + "::" + pwd).encode()).hexdigest()
    resp = make_response("", 302)
    resp.headers["Location"] = "/"
    resp.set_cookie("radar_name", name, max_age=12 * 3600, samesite="Lax")
    resp.set_cookie("radar_uk", uk, max_age=12 * 3600, httponly=True, samesite="Lax")
    return resp


# 兼容旧看板/常见路径，一律跳回首页，避免 404
@app.route("/login", methods=["GET"])
@app.route("/info")
@app.route("/index.html")
def _legacy_redirect():
    resp = make_response("", 302)
    resp.headers["Location"] = "/"
    return resp


@app.errorhandler(404)
def _not_found(e):
    # API / 静态文件保持 404 语义；其余未知页面路径统一跳回首页
    p = request.path or ""
    if p.startswith("/api/") or p.startswith("/static/"):
        return jsonify({"error": "not found"}), 404
    resp = make_response("", 302)
    resp.headers["Location"] = "/"
    return resp


@app.route("/api/me")
def api_me():
    name = request.cookies.get("radar_name")
    uk = request.cookies.get("radar_uk")
    if not name or not uk:
        return jsonify({"error": "not logged in"}), 401
    return jsonify({"name": name, "user_key": uk})


@app.route("/api/logout")
def api_logout():
    resp = make_response("", 302)
    resp.headers["Location"] = "/"
    resp.delete_cookie("radar_name")
    resp.delete_cookie("radar_uk")
    return resp


@app.route("/tracker")
def tracker():
    # 记录工具：未登录先去登录页
    if not request.cookies.get("radar_uk"):
        resp = make_response("", 302)
        resp.headers["Location"] = "/"
        return resp
    return send_from_directory(app.static_folder, "index.html")


@app.route("/campus")
def campus():
    # 兼容旧链接
    name = request.cookies.get("radar_name")
    uk = request.cookies.get("radar_uk")
    if not name or not uk:
        return make_response(_LOGIN_GATE.replace("__ERR__", ""), 200)
    prof = load_profile(uk)
    if not prof:
        resp = make_response("", 302)
        resp.headers["Location"] = "/setup"
        return resp
    return _campus_page(name, prof)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")), threaded=True)
