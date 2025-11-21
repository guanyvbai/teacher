import os
import time
import fcntl
from datetime import date, datetime, timedelta
from secrets import token_urlsafe

from dateutil.relativedelta import relativedelta
from flask import (
    Flask, render_template, request, redirect, url_for, flash, jsonify,
    abort, g, session
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from flask_login import (
    LoginManager, login_user, login_required, logout_user, current_user, UserMixin
)

# ---------- 基础 ----------
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"   # 未登录时重定向到 /login

STAGE_CHOICES = ["初一", "初二", "初三", "高一", "高二", "高三", "其他"]
ROLE_SUPERADMIN = "superadmin"
ROLE_TEACHER = "teacher"
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_CHOICES = [
    (ROLE_SUPERADMIN, "超级管理员（全部权限，可管理账号）"),
    (ROLE_TEACHER, "老师/管理员（全部业务权限）"),
    (ROLE_ADMIN, "管理员（仅学生/课时/报表）"),
    (ROLE_VIEWER, "家长展示账号（只读）"),
]

# ---------- 模型 ----------
def bootstrap_db_once():
    """
    通过文件锁确保只会有一个进程执行 DB 初始化（create_all/seed）。
    在容器 Linux 环境可用；Windows 主机不影响，因为代码运行在 Linux 容器中。
    """
    lock_path = os.getenv("INIT_LOCK_FILE", "/tmp/app_init.lock")
    with open(lock_path, "w") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        try:
            db.create_all()
            ensure_stage_column()
            ensure_admin_user()
            seed_if_empty()
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_TEACHER)  # 可扩展: admin/editor/viewer

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

@login_manager.user_loader
def load_user(uid):
    return User.query.get(int(uid))

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    hourly_rate = db.Column(db.Float, nullable=False, default=100.0)
    stage = db.Column(db.String(50), nullable=False, default="其他")
    sessions = db.relationship("Session", backref="student", cascade="all, delete-orphan")
    lessons = db.relationship("Lesson", backref="student", cascade="all, delete-orphan")

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    hours = db.Column(db.Float, nullable=False)

class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_hours = db.Column(db.Float, nullable=False, default=1.0)
    note = db.Column(db.String(200), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="planned")  # planned/done

# ---------- 工具 ----------
def lesson_overlaps_any(start_at: datetime, duration_hours: float, exclude_lid: int | None = None):
    """
    跨学生检测冲突：同一老师同一时间只能上一节课。
    判定： [start, end) 与现有 [s, e) 区间有交集。
    """
    end_at = start_at + timedelta(hours=duration_hours)
    day_start = datetime.combine(start_at.date(), datetime.min.time())
    day_end = day_start + timedelta(days=1)

    q = (Lesson.query
         .filter(Lesson.start_at >= day_start, Lesson.start_at < day_end))
    if exclude_lid:
        q = q.filter(Lesson.id != exclude_lid)

    conflicts = []
    for l in q.all():
        l_end = l.start_at + timedelta(hours=l.duration_hours)
        if not (end_at <= l.start_at or start_at >= l_end):
            conflicts.append(l)
    return conflicts


def _permissions(user):
    role = getattr(user, "role", None)
    can_schedule = role in {ROLE_SUPERADMIN, ROLE_TEACHER}
    can_manage_students = role in {ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN}
    can_manage_users = role == ROLE_SUPERADMIN
    can_sessions = role in {ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN}
    readonly = role == ROLE_VIEWER
    return {
        "role": role,
        "manage_users": can_manage_users,
        "manage_students": can_manage_students,
        "manage_sessions": can_sessions,
        "schedule": can_schedule,
        "readonly": readonly,
    }


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return deco
    
def _get_year_month():
    y = request.args.get("year", type=int)
    m = request.args.get("month", type=int)
    today = date.today()
    if not y or not m:
        y, m = today.year, today.month
    return y, m

def month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = start + relativedelta(months=1)
    return start, end

def iter_dates(start: date, end: date):
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)

def build_calendar(year: int, month: int):
    start, end = month_bounds(year, month)
    first_weekday = start.weekday()
    grid_start = start - timedelta(days=first_weekday)
    cells, d = [], grid_start
    for _ in range(42):
        cells.append({"date": d, "in_month": (start <= d < end)})
        d += timedelta(days=1)
    return [cells[i:i + 7] for i in range(0, 42, 7)]

def group_students(students):
    buckets = {s: [] for s in STAGE_CHOICES}
    for st in students:
        buckets[(st.stage if st.stage in STAGE_CHOICES else "其他")].append(st)
    for arr in buckets.values():
        arr.sort(key=lambda x: x.name)
    return [(k, buckets[k]) for k in STAGE_CHOICES]

def ensure_stage_column():
    info = db.session.execute(text("PRAGMA table_info('student')")).fetchall()
    cols = [row[1] for row in info]
    if "stage" not in cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN stage VARCHAR(50) NOT NULL DEFAULT '其他'"))
        db.session.commit()

def ensure_admin_user():
    """
    首次启动时创建管理员用户：
    环境变量:
      ADMIN_USER (默认: admin)
      ADMIN_PASS (默认: admin123  —— 上线时请务必改掉！)
    """
    username = os.getenv("ADMIN_USER", "admin")
    password = os.getenv("ADMIN_PASS", "admin123")
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, role=ROLE_SUPERADMIN)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        print(f"[INIT] Created admin user: {username}  (请尽快修改密码)")

# ---- 简易 CSRF（对所有 POST 生效） ----
def _csrf_ensure():
    if request.method == "POST":
        token_form = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        token_sess = session.get("csrf_token")
        if not token_form or not token_sess or token_form != token_sess:
            abort(400, description="Bad CSRF token")

def _get_or_make_csrf_token():
    tok = session.get("csrf_token")
    if not tok:
        tok = token_urlsafe(32)
        session["csrf_token"] = tok
    return tok

# ---------- 应用工厂 ----------
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-in-prod")
    db_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SQLite 更友好（并发/锁）
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"timeout": 15, "check_same_thread": False}
    }

    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
            bootstrap_db_once()

    # 全局：侧边栏分组学生 + 注入 csrf_token
    @app.context_processor
    def inject_globals():
        groups = group_students(Student.query.order_by(Student.name).all())
        perms = _permissions(current_user)
        return {
            "sidebar_groups": groups,
            "STAGE_CHOICES": STAGE_CHOICES,
            "csrf_token": _get_or_make_csrf_token,
            "PERMS": perms,
            "ROLE_CHOICES": ROLE_CHOICES,
        }

    # 请求计时 + CSRF 保护（白名单放行）
    @app.before_request
    def _tic_and_csrf():
        g._t0 = time.time()
        # 允许无需登录/CSRF 的路径
        open_paths = {"/login", "/healthz"}
        if request.path.startswith("/static/"):
            return
        # CSRF
        if request.method == "POST" and request.path not in open_paths:
            _csrf_ensure()
        # 只读账号：拒绝除登录/退出以外的 POST
        if current_user.is_authenticated and current_user.role == ROLE_VIEWER:
            if request.method == "POST" and request.path not in {"/logout"}:
                abort(403)

    @app.after_request
    def _toc(resp):
        try:
            dt = time.time() - getattr(g, "_t0", time.time())
            if dt > 1.0:
                app.logger.warning("SLOW %.3fs %s %s", dt, request.method, request.path)
        except Exception:
            pass
        # 安全响应头（基础版）
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    # 健康检查
    @app.route("/healthz")
    def healthz():
        return "ok", 200

    # ---- 登录/退出 ----
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user, remember=True)
                flash("登录成功", "ok")
                next_url = request.args.get("next") or url_for("dashboard")
                return redirect(next_url)
            else:
                flash("用户名或密码错误", "error")
        return render_template("login.html", page_title="登录")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        flash("已退出登录", "ok")
        return redirect(url_for("login"))

    # ---- 账号管理（仅超级管理员） ----
    @app.route("/users")
    @login_required
    @role_required(ROLE_SUPERADMIN)
    def user_list():
        users = User.query.order_by(User.username.asc()).all()
        return render_template("users.html", users=users, page_title="账号管理")

    @app.route("/users/new", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN)
    def user_new():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", ROLE_TEACHER)
            if not username or not password:
                flash("用户名和密码均不能为空", "error")
                return render_template("new_user.html", page_title="新增账号")
            if role not in {r for r, _ in ROLE_CHOICES}:
                flash("角色不合法", "error")
                return render_template("new_user.html", page_title="新增账号")
            exists = User.query.filter_by(username=username).first()
            if exists:
                flash("用户名已存在", "error")
                return render_template("new_user.html", page_title="新增账号")
            u = User(username=username, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash("账号已创建", "ok")
            return redirect(url_for("user_list"))
        return render_template("new_user.html", page_title="新增账号")

    @app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN)
    def user_edit(uid):
        user = User.query.get_or_404(uid)
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            role = request.form.get("role", user.role)
            new_password = request.form.get("password", "")
            if not username:
                flash("用户名不能为空", "error")
                return render_template("edit_user.html", user=user, page_title="编辑账号")
            if role not in {r for r, _ in ROLE_CHOICES}:
                flash("角色不合法", "error")
                return render_template("edit_user.html", user=user, page_title="编辑账号")
            conflict = User.query.filter_by(username=username).first()
            if conflict and conflict.id != user.id:
                flash("已存在同名账号", "error")
                return render_template("edit_user.html", user=user, page_title="编辑账号")
            user.username = username
            user.role = role
            if new_password:
                user.set_password(new_password)
            db.session.commit()
            flash("账号已更新", "ok")
            return redirect(url_for("user_list"))
        return render_template("edit_user.html", user=user, page_title="编辑账号")

    @app.route("/users/<int:uid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN)
    def user_delete(uid):
        user = User.query.get_or_404(uid)
        if current_user.id == user.id:
            flash("不能删除当前登录账号", "error")
            return redirect(url_for("user_list"))
        db.session.delete(user)
        db.session.commit()
        flash("账号已删除", "ok")
        return redirect(url_for("user_list"))

    # ---- 首页：双日历（需登录） ----
    @app.route("/")
    @login_required
    def dashboard():
        year, month = _get_year_month()
        cal = build_calendar(year, month)
        start_m, end_m = month_bounds(year, month)

        # ① 当月 Session 汇总（课时费日历）
        sessions = (Session.query.join(Student)
                    .filter(Session.date >= start_m, Session.date < end_m).all())
        per_day = {d: {"hours": 0.0, "fee": 0.0} for d in iter_dates(start_m, end_m)}
        total_hours = total_fee = 0.0
        for s in sessions:
            per_day[s.date]["hours"] += s.hours
            fee = s.hours * s.student.hourly_rate
            per_day[s.date]["fee"] += fee
            total_hours += s.hours
            total_fee += fee

        # ② 当月 Lesson 事件（日历）
        lessons = (Lesson.query.join(Student)
                   .filter(Lesson.start_at >= datetime.combine(start_m, datetime.min.time()))
                   .filter(Lesson.start_at < datetime.combine(end_m, datetime.min.time()))
                   .order_by(Lesson.start_at.asc()).all())
        lessons_by_date = {}
        for l in lessons:
            d = l.start_at.date()
            lessons_by_date.setdefault(d, []).append(l)

        prev = start_m - relativedelta(months=1)
        nextm = start_m + relativedelta(months=1)
        return render_template(
            "dashboard.html",
            year=year, month=month, cal=cal,
            per_day=per_day,
            lessons_by_date=lessons_by_date,
            total_hours=round(total_hours, 2), total_fee=round(total_fee, 2),
            prev_year=prev.year, prev_month=prev.month,
            next_year=nextm.year, next_month=nextm.month,
            page_title="本月总览：课时费 & 课程表"
        )

    # ---- 学生页：双日历（需登录） ----
    @app.route("/students/<int:sid>")
    @login_required
    def student_dashboard(sid):
        student = Student.query.get_or_404(sid)
        year, month = _get_year_month()
        cal = build_calendar(year, month)
        start, end = month_bounds(year, month)

        sessions = (Session.query
                    .filter(Session.student_id == sid, Session.date >= start, Session.date < end)
                    .order_by(Session.date.asc()).all())
        per_day = {d: {"hours": 0.0, "fee": 0.0} for d in iter_dates(start, end)}
        total_hours = total_fee = 0.0
        for s in sessions:
            per_day[s.date]["hours"] += s.hours
            fee = s.hours * student.hourly_rate
            per_day[s.date]["fee"] += fee
            total_hours += s.hours
            total_fee += fee

        lessons = (Lesson.query
                   .filter(Lesson.student_id == sid)
                   .filter(Lesson.start_at >= datetime.combine(start, datetime.min.time()))
                   .filter(Lesson.start_at < datetime.combine(end, datetime.min.time()))
                   .order_by(Lesson.start_at.asc()).all())
        lessons_by_date = {}
        for l in lessons:
            d = l.start_at.date()
            lessons_by_date.setdefault(d, []).append(l)

        prev = start - relativedelta(months=1)
        nextm = start + relativedelta(months=1)
        return render_template(
            "student_dashboard.html",
            student=student, year=year, month=month, cal=cal,
            per_day=per_day, lessons_by_date=lessons_by_date,
            sessions=sessions,
            total_hours=round(total_hours, 2), total_fee=round(total_fee, 2),
            prev_year=prev.year, prev_month=prev.month,
            next_year=nextm.year, next_month=nextm.month,
            page_title=f"{student.name} 的月度概览"
        )

    # ---- 学生管理（需登录） ----
    @app.route("/students")
    @login_required
    def students():
        q = request.args.get("q", "").strip()
        data = (Student.query.filter(Student.name.ilike(f"%{q}%")).all()
                if q else Student.query.all())
        groups = group_students(data)
        return render_template("students.html", groups=groups, q=q, page_title="学生列表")

    @app.route("/students/new", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def new_student():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            rate = request.form.get("hourly_rate", "").strip()
            stage = request.form.get("stage", "其他")
            try:
                rate_val = float(rate)
            except Exception:
                flash("小时费率必须是数字", "error")
                return render_template("new_student.html", page_title="添加学生")
            if not name:
                flash("姓名不能为空", "error")
                return render_template("new_student.html", page_title="添加学生")
            exists = Student.query.filter_by(name=name).first()
            if exists:
                flash("已存在同名学生，请更换姓名", "error")
                return render_template("new_student.html", page_title="添加学生")
            s = Student(name=name, hourly_rate=rate_val, stage=stage)
            db.session.add(s)
            db.session.commit()
            flash("学生已添加", "ok")
            return redirect(url_for("student_dashboard", sid=s.id))
        return render_template("new_student.html", page_title="添加学生")

    @app.route("/students/<int:sid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def edit_student(sid):
        s = Student.query.get_or_404(sid)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            rate = request.form.get("hourly_rate", "").strip()
            stage = request.form.get("stage", "其他")
            try:
                s.hourly_rate = float(rate)
            except Exception:
                flash("小时费率必须是数字", "error")
                return render_template("edit_student.html", student=s, page_title="编辑学生")
            if not name:
                flash("姓名不能为空", "error")
                return render_template("edit_student.html", student=s, page_title="编辑学生")
            conflict = Student.query.filter_by(name=name).first()
            if conflict and conflict.id != s.id:
                flash("已存在同名学生，请更换姓名", "error")
                return render_template("edit_student.html", student=s, page_title="编辑学生")
            s.name = name
            s.stage = stage
            db.session.commit()
            flash("学生信息已更新", "ok")
            return redirect(url_for("student_dashboard", sid=s.id))
        return render_template("edit_student.html", student=s, page_title="编辑学生")

    @app.route("/students/<int:sid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def delete_student(sid):
        s = Student.query.get_or_404(sid)
        db.session.delete(s)
        db.session.commit()
        flash("学生已删除", "ok")
        return redirect(url_for("students"))

    # ---- 已上课 Session（需登录） ----
    @app.route("/students/<int:sid>/add_session", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def add_session(sid):
        student = Student.query.get_or_404(sid)
        date_str = request.form.get("date")
        hours_str = request.form.get("hours")
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            h = float(hours_str)
            if h <= 0:
                raise ValueError("hours must be positive")
        except Exception:
            flash("请提供正确的日期与课时（正数）", "error")
            return redirect(url_for("student_dashboard", sid=sid))
        db.session.add(Session(student_id=student.id, date=d, hours=h))
        db.session.commit()
        flash("课时记录已添加", "ok")
        return redirect(url_for("student_dashboard", sid=sid, year=d.year, month=d.month))

    @app.route("/sessions/<int:sid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def delete_session(sid):
        sess = Session.query.get_or_404(sid)
        stid = sess.student_id
        y, m = sess.date.year, sess.date.month
        db.session.delete(sess)
        db.session.commit()
        flash("记录已删除", "ok")
        return redirect(url_for("student_dashboard", sid=stid, year=y, month=m))

    # ---- 课程表 Lesson（需登录） ----
    @app.route("/lessons/add", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def add_lesson():
        try:
            student_id_raw = request.form.get("student_id", "").strip()
            if not student_id_raw:
                raise ValueError("missing student_id")
            student_id = int(student_id_raw)

            date_str = request.form["lesson_date"]
            time_str = request.form["lesson_time"]
            duration = float(request.form["duration"])
            note = request.form.get("note", "").strip()
            start_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if duration <= 0:
                raise ValueError("duration must be positive")
        except Exception:
            flash("请正确填写课程表信息（学生/日期/时间/时长）", "error")
            return redirect(url_for("dashboard"))

        # ✅ 跨学生冲突硬校验
        conflicts = lesson_overlaps_any(start_at, duration)
        if conflicts:
            human = []
            for c in conflicts:
                c_end = c.start_at + timedelta(hours=c.duration_hours)
                who = c.student.name if c.student else "未知学生"
                human.append(f"{who}：{c.start_at.strftime('%m/%d %H:%M')}-{c_end.strftime('%H:%M')}")
            flash(f"排课与以下安排冲突：{'; '.join(human)}", "error")
            return redirect(url_for("dashboard", year=start_at.year, month=start_at.month))

        db.session.add(Lesson(student_id=student_id, start_at=start_at, duration_hours=duration, note=note))
        db.session.commit()
        flash("已添加到课程表", "ok")
        return redirect(url_for("dashboard", year=start_at.year, month=start_at.month))

    @app.route("/lessons/<int:lid>/done", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_done(lid):
        l = Lesson.query.get_or_404(lid)
        y, m = l.start_at.year, l.start_at.month
        if l.status != "done":
            lesson_date = l.start_at.date()
            existing = (Session.query
                        .filter_by(student_id=l.student_id, date=lesson_date)
                        .first())
            if existing:
                existing.hours = float(existing.hours) + float(l.duration_hours)
            else:
                db.session.add(Session(
                    student_id=l.student_id, date=lesson_date, hours=float(l.duration_hours)
                ))
            l.status = "done"
            db.session.commit()
            flash("已标记完成，并记入当日课时", "ok")
        else:
            flash("该排课已完成（跳过重复记账）", "ok")
        return redirect(url_for("dashboard", year=y, month=m))

    @app.route("/lessons/<int:lid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_delete(lid):
        l = Lesson.query.get_or_404(lid)
        y, m = l.start_at.year, l.start_at.month
        db.session.delete(l)
        db.session.commit()
        flash("已从课程表删除", "ok")
        return redirect(url_for("dashboard", year=y, month=m))

    # ---- API：当天 Lesson 列表（需登录） ----
    @app.route("/api/lessons")
    @login_required
    def api_lessons_by_day():
        date_str = request.args.get("date")
        sid = request.args.get("sid", type=int)
        if not date_str:
            return jsonify({"ok": False, "error": "missing date"}), 400
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "error": "bad date"}), 400

        start_dt = datetime.combine(day, datetime.min.time())
        end_dt = datetime.combine(day + timedelta(days=1), datetime.min.time())

        q = Lesson.query.join(Student).filter(Lesson.start_at >= start_dt, Lesson.start_at < end_dt)
        if sid:
            q = q.filter(Lesson.student_id == sid)
        q = q.order_by(Lesson.start_at.asc())

        data = []
        for l in q.all():
            data.append({
                "id": l.id,
                "student_id": l.student_id,
                "student_name": l.student.name if l.student else "",
                "time": l.start_at.strftime("%H:%M"),
                "duration": round(l.duration_hours, 2),
                "note": l.note or "",
                "status": l.status,
                "ymd": day.isoformat(),
            })
        return jsonify({"ok": True, "items": data})
    @app.route("/api/lessons/check_conflict")
    @login_required
    def api_check_conflict():
        """
        GET /api/lessons/check_conflict?date=YYYY-MM-DD&time=HH:MM&duration=1.5
        返回 { ok: True, conflicts: [{student:"张三", start:"HH:MM", end:"HH:MM"}...] }
        """
        try:
            date_str = request.args.get("date", "")
            time_str = request.args.get("time", "")
            duration = float(request.args.get("duration", "0"))
            if not (date_str and time_str and duration > 0):
                return jsonify({"ok": False, "error": "missing params"}), 400
            start_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            return jsonify({"ok": False, "error": "bad params"}), 400

        conflicts = lesson_overlaps_any(start_at, duration)
        items = []
        for c in conflicts:
            ce = c.start_at + timedelta(hours=c.duration_hours)
            items.append({
                "student": c.student.name if c.student else "",
                "start": c.start_at.strftime("%H:%M"),
                "end": ce.strftime("%H:%M")
            })
        return jsonify({"ok": True, "conflicts": items})
        
    return app

# ---------- 种子数据 ----------
def seed_if_empty():
    if Student.query.count() == 0:
        alice = Student(name="Alice", hourly_rate=120.0, stage="高中")
        bob = Student(name="Bob", hourly_rate=90.0, stage="初中")
        carol = Student(name="Carol", hourly_rate=100.0, stage="小学")
        db.session.add_all([alice, bob, carol])
        db.session.flush()
        today = date.today()
        sessions = [
            Session(student_id=alice.id, date=today.replace(day=min(2, 28)), hours=1.5),
            Session(student_id=alice.id, date=today.replace(day=min(10, 28)), hours=2.0),
            Session(student_id=bob.id,   date=today.replace(day=min(5, 28)), hours=1.0),
            Session(student_id=carol.id, date=today.replace(day=min(7, 28)), hours=2.5),
        ]
        db.session.add_all(sessions)
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        lessons = [
            Lesson(student_id=alice.id, start_at=now + timedelta(days=1, hours=2), duration_hours=1.5, note="冲刺数学"),
            Lesson(student_id=bob.id,   start_at=now + timedelta(days=2, hours=1), duration_hours=1.0, note="英语口语"),
            Lesson(student_id=carol.id, start_at=now + timedelta(days=3, hours=3), duration_hours=2.0, note="阅读训练"),
        ]
        db.session.add_all(lessons)
        db.session.commit()

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
