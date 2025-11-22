import os
import time
import fcntl
import csv
import io
from datetime import date, datetime, timedelta
from secrets import token_urlsafe

from dateutil.relativedelta import relativedelta
from flask import (
    Flask, render_template, request, redirect, url_for, flash, jsonify,
    abort, g, session, Response
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

STAGE_CHOICES = ["其他", "初一", "初二", "初三", "高一", "高二", "高三"]
LESSON_STATUS_PLANNED = "planned"
LESSON_STATUS_DONE = "done"
LESSON_STATUS_CANCELLED = "cancelled"
CHARGE_MODE_PREPAID = "prepaid_hours"
CHARGE_MODE_PAY_PER_LESSON = "pay_per_lesson"
CHARGE_MODE_MONTHLY = "monthly_settlement"
CHARGE_MODE_CHOICES = [
    (CHARGE_MODE_PREPAID, "预付课时"),
    (CHARGE_MODE_PAY_PER_LESSON, "一课一付"),
    (CHARGE_MODE_MONTHLY, "合作机构月结"),
]
ROLE_SUPERADMIN = "superadmin"
ROLE_TEACHER = "teacher"
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_CHOICES = [
    (ROLE_SUPERADMIN, "超级管理员（全部权限，可管理账号）"),
    (ROLE_TEACHER, "老师（全部业务权限）"),
    (ROLE_ADMIN, "管理员（仅学生/课时/报表）"),
    (ROLE_VIEWER, "家长展示账号（只读）"),
]
ROLE_LABELS = dict(ROLE_CHOICES)

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
            ensure_student_finance_columns()
            ensure_session_columns()
            ensure_lesson_status_column()
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
    charge_mode = db.Column(db.String(30), nullable=False, default=CHARGE_MODE_PAY_PER_LESSON)
    remaining_hours = db.Column(db.Float, nullable=False, default=0.0)
    outstanding_amount = db.Column(db.Float, nullable=False, default=0.0)
    balance_amount = db.Column(db.Float, nullable=False, default=0.0)
    sessions = db.relationship("Session", backref="student", cascade="all, delete-orphan")
    lessons = db.relationship("Lesson", backref="student", cascade="all, delete-orphan")
    payments = db.relationship("PaymentRecord", backref="student", cascade="all, delete-orphan")
    exams = db.relationship("ExamRecord", backref="student", cascade="all, delete-orphan")

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    hours = db.Column(db.Float, nullable=False)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        default=datetime.utcnow,
    )
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=True, index=True)
    absent = db.Column(db.Boolean, nullable=False, default=False)
    content = db.Column(db.String(255), nullable=False, default="")
    feedback = db.Column(db.Text, nullable=True)
    subject = db.Column(db.String(80), nullable=False, default="综合")
    charged_amount = db.Column(db.Float, nullable=False, default=0.0)
    deducted_hours = db.Column(db.Float, nullable=False, default=0.0)
    outstanding_amount = db.Column(db.Float, nullable=False, default=0.0)

class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_hours = db.Column(db.Float, nullable=False, default=1.0)
    note = db.Column(db.String(200), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=LESSON_STATUS_PLANNED)  # planned/done/cancelled


class PaymentRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(80), nullable=False, default="现金")
    operator = db.Column(db.String(80), nullable=False, default="")
    note = db.Column(db.String(255), nullable=False, default="")
    category = db.Column(db.String(40), nullable=False, default=CHARGE_MODE_PREPAID)
    created_at = db.Column(db.DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class ExamRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False, index=True)
    exam_date = db.Column(db.Date, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    score = db.Column(db.Float, nullable=False)
    essay_score = db.Column(db.Float, nullable=True)

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
         .filter(Lesson.start_at >= day_start, Lesson.start_at < day_end)
         .filter(Lesson.status != LESSON_STATUS_CANCELLED))
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
        "role_label": ROLE_LABELS.get(role, role or "未知角色"),
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


def ensure_student_finance_columns():
    info = db.session.execute(text("PRAGMA table_info('student')")).fetchall()
    cols = [row[1] for row in info]
    altered = False
    if "charge_mode" not in cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN charge_mode VARCHAR(30) NOT NULL DEFAULT 'pay_per_lesson'"))
        altered = True
    if "remaining_hours" not in cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN remaining_hours FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if "outstanding_amount" not in cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN outstanding_amount FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if "balance_amount" not in cols:
        db.session.execute(text("ALTER TABLE student ADD COLUMN balance_amount FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if altered:
        db.session.commit()


def ensure_session_columns():
    info = db.session.execute(text("PRAGMA table_info('session')")).fetchall()
    cols = [row[1] for row in info]
    altered = False
    if "created_at" not in cols:
        # SQLite 不支持在 ALTER TABLE 时使用 CURRENT_TIMESTAMP 作为非常量默认值
        # 这里先追加可空字段，再用 UPDATE 补齐历史数据，插入时靠模型 default 保证非空
        db.session.execute(text("ALTER TABLE session ADD COLUMN created_at DATETIME"))
      
        altered = True
    if "lesson_id" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN lesson_id INTEGER"))
        altered = True
    if "absent" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN absent INTEGER NOT NULL DEFAULT 0"))
        altered = True
    if "content" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN content VARCHAR(255) NOT NULL DEFAULT ''"))
        altered = True
    if "feedback" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN feedback TEXT"))
        altered = True
    if "subject" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN subject VARCHAR(80) NOT NULL DEFAULT '综合'"))
        altered = True
    if "charged_amount" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN charged_amount FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if "deducted_hours" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN deducted_hours FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if "outstanding_amount" not in cols:
        db.session.execute(text("ALTER TABLE session ADD COLUMN outstanding_amount FLOAT NOT NULL DEFAULT 0"))
        altered = True
    if altered:
        db.session.commit()
    db.session.execute(text("UPDATE session SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"))
    db.session.execute(text("UPDATE session SET absent = COALESCE(absent, 0)"))
    db.session.execute(text("UPDATE session SET content = COALESCE(content, '')"))
    db.session.execute(text("UPDATE session SET subject = COALESCE(subject, '综合')"))
    db.session.commit()


def ensure_lesson_status_column():
    info = db.session.execute(text("PRAGMA table_info('lesson')")).fetchall()
    cols = [row[1] for row in info]
    if "status" not in cols:
        db.session.execute(text("ALTER TABLE lesson ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'planned'"))
        db.session.commit()


def _apply_payment_effect(student: Student, record: "PaymentRecord"):
    amount = float(record.amount or 0.0)
    rate = student.hourly_rate or 1
    if record.category == CHARGE_MODE_PREPAID:
        student.remaining_hours = round((student.remaining_hours or 0.0) + amount / rate, 2)
    elif record.category == "arrears_payment":
        prev = student.outstanding_amount or 0.0
        student.outstanding_amount = max(0.0, round(prev - amount, 2))
        leftover = amount - prev
        if leftover > 0:
            student.balance_amount = round((student.balance_amount or 0.0) + leftover, 2)
    elif record.category == "balance_topup":
        student.balance_amount = round((student.balance_amount or 0.0) + amount, 2)


def _revert_payment_effect(student: Student, record: "PaymentRecord"):
    amount = float(record.amount or 0.0)
    rate = student.hourly_rate or 1
    if record.category == CHARGE_MODE_PREPAID:
        student.remaining_hours = max(0.0, (student.remaining_hours or 0.0) - amount / rate)
    elif record.category == "arrears_payment":
        student.outstanding_amount = round((student.outstanding_amount or 0.0) + amount, 2)
    elif record.category == "balance_topup":
        student.balance_amount = max(0.0, (student.balance_amount or 0.0) - amount)

def ensure_admin_user():
    """
    首次启动时创建管理员用户：
    环境变量:
      ADMIN_USER (默认: master)
      ADMIN_PASS (默认: master123  —— 上线时请务必改掉！)
    """
    username = os.getenv("ADMIN_USER", "master")
    password = os.getenv("ADMIN_PASS", "master123")
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, role=ROLE_SUPERADMIN)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        print(f"[INIT] Created admin user: {username}  (请尽快修改密码)")


def ensure_due_lessons_recorded(now: datetime | None = None):
    """自动把已结束的排课转为课时记录，并标记状态为 done。"""
    now = now or datetime.now()
    planned = Lesson.query.filter(Lesson.status == LESSON_STATUS_PLANNED, Lesson.start_at <= now).all()
    changed = False
    for l in planned:
        end_at = l.start_at + timedelta(hours=l.duration_hours)
        if end_at > now:
            continue
        sess = Session.query.filter_by(lesson_id=l.id).first()
        if not sess:
            sess = Session(
                student_id=l.student_id,
                date=l.start_at.date(),
                hours=float(l.duration_hours),
                lesson_id=l.id,
                absent=False,
            )
            db.session.add(sess)
        l.status = LESSON_STATUS_DONE
        changed = True
    if changed:
        db.session.commit()


def is_session_editable(sess: Session) -> bool:
    created = sess.created_at or datetime.utcnow()
    return datetime.utcnow() - created <= timedelta(hours=24)


def ensure_session_for_lesson(lesson: Lesson):
    sess = Session.query.filter_by(lesson_id=lesson.id).first()
    if sess:
        sess.hours = float(lesson.duration_hours)
        sess.absent = False
        sess.date = lesson.start_at.date()
        refresh_session_finance(sess)
    else:
        sess = Session(
            student_id=lesson.student_id,
            date=lesson.start_at.date(),
            hours=float(lesson.duration_hours),
            lesson_id=lesson.id,
            absent=False,
        )
        db.session.add(sess)
        refresh_session_finance(sess)


def remove_session_for_lesson(lesson: Lesson):
    sess = Session.query.filter_by(lesson_id=lesson.id).first()
    if sess:
        if sess.student:
            _revert_finance_effect(sess.student, sess)
        db.session.delete(sess)


def _revert_finance_effect(student: Student, sess: Session):
    student.remaining_hours = max(0.0, (student.remaining_hours or 0.0) + float(sess.deducted_hours or 0.0))
    student.outstanding_amount = max(0.0, (student.outstanding_amount or 0.0) - float(sess.outstanding_amount or 0.0))
    sess.deducted_hours = 0.0
    sess.outstanding_amount = 0.0
    sess.charged_amount = 0.0


def _apply_finance_effect(student: Student, sess: Session):
    if sess.absent:
        sess.deducted_hours = 0.0
        sess.outstanding_amount = 0.0
        sess.charged_amount = 0.0
        return

    hours = float(sess.hours or 0.0)
    rate = float(student.hourly_rate or 0.0)
    sess.charged_amount = round(hours * rate, 2)

    mode = student.charge_mode or CHARGE_MODE_PAY_PER_LESSON
    remaining = float(student.remaining_hours or 0.0)

    if mode == CHARGE_MODE_PREPAID:
        if remaining >= hours:
            sess.deducted_hours = hours
            student.remaining_hours = round(remaining - hours, 2)
            sess.outstanding_amount = 0.0
        else:
            sess.deducted_hours = remaining
            owed_hours = hours - remaining
            owed_amount = round(owed_hours * rate, 2)
            student.remaining_hours = 0.0
            sess.outstanding_amount = owed_amount
            student.outstanding_amount = round((student.outstanding_amount or 0.0) + owed_amount, 2)
    else:
        sess.deducted_hours = 0.0
        sess.outstanding_amount = round(hours * rate, 2)
        student.outstanding_amount = round((student.outstanding_amount or 0.0) + sess.outstanding_amount, 2)


def refresh_session_finance(sess: Session):
    student = sess.student
    if not student:
        return
    _revert_finance_effect(student, sess)
    _apply_finance_effect(student, sess)

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
            "ROLE_LABELS": ROLE_LABELS,
            "is_session_editable": is_session_editable,
            "CHARGE_MODE_CHOICES": CHARGE_MODE_CHOICES,
            "CHARGE_MODE_PREPAID": CHARGE_MODE_PREPAID,
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
        ensure_due_lessons_recorded()

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
            fee = s.charged_amount if s.charged_amount else s.hours * s.student.hourly_rate
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

    @app.route("/parent/schedule")
    @login_required
    def parent_schedule():
        year, month = _get_year_month()
        cal = build_calendar(year, month)
        start_m, end_m = month_bounds(year, month)
        lessons = (Lesson.query.join(Student)
                   .filter(Lesson.start_at >= datetime.combine(start_m, datetime.min.time()))
                   .filter(Lesson.start_at < datetime.combine(end_m, datetime.min.time()))
                   .order_by(Lesson.start_at.asc()).all())
        lessons_by_date = {}
        for l in lessons:
            d = l.start_at.date()
            lessons_by_date.setdefault(d, []).append(l)

        upcoming = (Lesson.query.join(Student)
                    .filter(Lesson.start_at >= datetime.combine(date.today(), datetime.min.time()))
                    .order_by(Lesson.start_at.asc()).limit(50).all())

        recent_sessions = (Session.query.join(Student)
                            .filter(Session.date >= start_m - relativedelta(months=1))
                            .order_by(Session.date.desc()).limit(30).all())

        prev = start_m - relativedelta(months=1)
        nextm = start_m + relativedelta(months=1)
        return render_template(
            "parent_schedule.html",
            year=year, month=month, cal=cal,
            lessons_by_date=lessons_by_date,
            prev_year=prev.year, prev_month=prev.month,
            next_year=nextm.year, next_month=nextm.month,
            upcoming=upcoming,
            recent_sessions=recent_sessions,
            page_title="家长端：排课规划",
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
            fee = s.charged_amount if s.charged_amount else s.hours * student.hourly_rate
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
        payments = (PaymentRecord.query.filter_by(student_id=sid)
                    .order_by(PaymentRecord.date.desc(), PaymentRecord.id.desc()).limit(50).all())
        exams = (ExamRecord.query.filter_by(student_id=sid)
                 .order_by(ExamRecord.exam_date.desc()).all())
        history_sessions = (Session.query.filter(Session.student_id == sid)
                             .order_by(Session.date.desc(), Session.id.desc()).limit(15).all())

        return render_template(
            "student_dashboard.html",
            student=student, year=year, month=month, cal=cal,
            per_day=per_day, lessons_by_date=lessons_by_date,
            sessions=sessions, payments=payments, exams=exams,
            history_sessions=history_sessions,
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
            charge_mode = request.form.get("charge_mode", CHARGE_MODE_PAY_PER_LESSON)
            remaining_raw = request.form.get("remaining_hours", "0").strip()
            outstanding_raw = request.form.get("outstanding_amount", "0").strip()
            balance_raw = request.form.get("balance_amount", "0").strip()
            try:
                rate_val = float(rate)
                remaining_val = float(remaining_raw or 0)
                outstanding_val = float(outstanding_raw or 0)
                balance_val = float(balance_raw or 0)
            except Exception:
                flash("小时费率与财务字段必须是数字", "error")
                return render_template("new_student.html", page_title="添加学生")
            if not name:
                flash("姓名不能为空", "error")
                return render_template("new_student.html", page_title="添加学生")
            exists = Student.query.filter_by(name=name).first()
            if exists:
                flash("已存在同名学生，请更换姓名", "error")
                return render_template("new_student.html", page_title="添加学生")
            s = Student(
                name=name,
                hourly_rate=rate_val,
                stage=stage,
                charge_mode=charge_mode,
                remaining_hours=remaining_val,
                outstanding_amount=outstanding_val,
                balance_amount=balance_val,
            )
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
            charge_mode = request.form.get("charge_mode", s.charge_mode)
            remaining_raw = request.form.get("remaining_hours", s.remaining_hours)
            outstanding_raw = request.form.get("outstanding_amount", s.outstanding_amount)
            balance_raw = request.form.get("balance_amount", s.balance_amount)
            try:
                s.hourly_rate = float(rate)
                s.remaining_hours = float(remaining_raw or 0)
                s.outstanding_amount = float(outstanding_raw or 0)
                s.balance_amount = float(balance_raw or 0)
            except Exception:
                flash("小时费率与财务字段必须是数字", "error")
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
            if charge_mode in {c for c, _ in CHARGE_MODE_CHOICES}:
                s.charge_mode = charge_mode
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
        content = request.form.get("content", "").strip()
        feedback = request.form.get("feedback", "").strip()
        subject = request.form.get("subject", "").strip() or "综合"
        absent = bool(request.form.get("absent"))
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            h = float(hours_str)
            if h <= 0 and not absent:
                raise ValueError("hours must be positive")
        except Exception:
            flash("请提供正确的日期与课时（正数）", "error")
            return redirect(url_for("student_dashboard", sid=sid))
        sess = Session(
            student_id=student.id,
            date=d,
            hours=(0 if absent else h),
            absent=absent,
            content=content,
            feedback=feedback,
            subject=subject,
        )
        db.session.add(sess)
        refresh_session_finance(sess)
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
        _revert_finance_effect(sess.student, sess)
        db.session.delete(sess)
        db.session.commit()
        flash("记录已删除", "ok")
        return redirect(url_for("student_dashboard", sid=stid, year=y, month=m))

    # ---- 缴费管理 ----
    @app.route("/students/<int:sid>/payments", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def add_payment(sid):
        student = Student.query.get_or_404(sid)
        try:
            dt = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
            amount = float(request.form.get("amount", 0))
            if amount <= 0:
                raise ValueError
        except Exception:
            flash("请输入正确的日期与金额", "error")
            return redirect(url_for("student_dashboard", sid=sid))
        category = request.form.get("category", CHARGE_MODE_PREPAID)
        if category not in {CHARGE_MODE_PREPAID, "arrears_payment", "balance_topup"}:
            category = CHARGE_MODE_PREPAID
        record = PaymentRecord(
            student_id=sid,
            date=dt,
            amount=amount,
            method=request.form.get("method", "现金"),
            operator=request.form.get("operator", current_user.username),
            note=request.form.get("note", ""),
            category=category,
        )
        _apply_payment_effect(student, record)
        db.session.add(record)
        db.session.commit()
        flash("缴费记录已添加并更新余额/欠费", "ok")
        return redirect(url_for("student_dashboard", sid=sid, year=dt.year, month=dt.month))

    @app.route("/payments/<int:pid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def edit_payment(pid):
        payment = PaymentRecord.query.get_or_404(pid)
        student = payment.student
        if request.method == "POST":
            try:
                date_val = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
                amount = float(request.form.get("amount", 0))
                if amount <= 0:
                    raise ValueError
            except Exception:
                flash("请输入正确的日期与金额", "error")
                return render_template("edit_payment.html", payment=payment, page_title="编辑缴费")
            _revert_payment_effect(student, payment)
            payment.date = date_val
            payment.amount = amount
            payment.method = request.form.get("method", payment.method)
            payment.operator = request.form.get("operator", payment.operator)
            payment.note = request.form.get("note", payment.note)
            category = request.form.get("category", payment.category)
            payment.category = category
            _apply_payment_effect(student, payment)
            db.session.commit()
            flash("缴费记录已更新", "ok")
            return redirect(url_for("student_dashboard", sid=student.id, year=payment.date.year, month=payment.date.month))
        return render_template("edit_payment.html", payment=payment, page_title="编辑缴费")

    @app.route("/payments/<int:pid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def delete_payment(pid):
        payment = PaymentRecord.query.get_or_404(pid)
        sid = payment.student_id
        _revert_payment_effect(payment.student, payment)
        db.session.delete(payment)
        db.session.commit()
        flash("缴费记录已删除", "ok")
        return redirect(url_for("student_dashboard", sid=sid))

    @app.route("/sessions/<int:sid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def edit_session(sid):
        sess = Session.query.get_or_404(sid)
        student = sess.student
        if not is_session_editable(sess):
            flash("超过24小时，无法修改课时，可删除后重新登记", "error")
            return redirect(url_for("student_dashboard", sid=student.id))
        if request.method == "POST":
            hours_raw = request.form.get("hours", "").strip()
            absent = bool(request.form.get("absent"))
            content = request.form.get("content", "").strip()
            feedback = request.form.get("feedback", "").strip()
            subject = request.form.get("subject", "").strip() or sess.subject or "综合"
            try:
                h = float(hours_raw or 0)
                if h <= 0 and not absent:
                    raise ValueError("hours must be positive")
            except Exception:
                flash("课时需为正数", "error")
                return render_template("edit_session.html", session=sess, student=student, page_title="编辑课时")
            _revert_finance_effect(student, sess)
            sess.hours = 0 if absent else h
            sess.absent = absent
            sess.content = content
            sess.feedback = feedback
            sess.subject = subject
            refresh_session_finance(sess)
            db.session.commit()
            flash("课时记录已更新", "ok")
            return redirect(url_for("student_dashboard", sid=student.id, year=sess.date.year, month=sess.date.month))
        return render_template("edit_session.html", session=sess, student=student, page_title="编辑课时")

    # ---- 成绩管理 ----
    @app.route("/students/<int:sid>/exams", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def add_exam(sid):
        student = Student.query.get_or_404(sid)
        try:
            exam_date = datetime.strptime(request.form.get("exam_date", ""), "%Y-%m-%d").date()
            score = float(request.form.get("score", 0))
        except Exception:
            flash("请正确填写考试日期和分数", "error")
            return redirect(url_for("student_dashboard", sid=sid))
        exam = ExamRecord(
            student_id=sid,
            exam_date=exam_date,
            name=request.form.get("name", "").strip() or "未命名考试",
            score=score,
            essay_score=request.form.get("essay_score", type=float),
        )
        db.session.add(exam)
        db.session.commit()
        flash("考试成绩已记录", "ok")
        return redirect(url_for("student_dashboard", sid=sid))

    @app.route("/exams/<int:eid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def edit_exam(eid):
        exam = ExamRecord.query.get_or_404(eid)
        if request.method == "POST":
            try:
                exam_date = datetime.strptime(request.form.get("exam_date", ""), "%Y-%m-%d").date()
                score = float(request.form.get("score", 0))
            except Exception:
                flash("请正确填写考试日期和分数", "error")
                return render_template("edit_exam.html", exam=exam, page_title="编辑考试")
            exam.exam_date = exam_date
            exam.name = request.form.get("name", exam.name).strip()
            exam.score = score
            exam.essay_score = request.form.get("essay_score", type=float)
            db.session.commit()
            flash("考试记录已更新", "ok")
            return redirect(url_for("student_dashboard", sid=exam.student_id))
        return render_template("edit_exam.html", exam=exam, page_title="编辑考试")

    @app.route("/exams/<int:eid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def delete_exam(eid):
        exam = ExamRecord.query.get_or_404(eid)
        sid = exam.student_id
        db.session.delete(exam)
        db.session.commit()
        flash("考试成绩已删除", "ok")
        return redirect(url_for("student_dashboard", sid=sid))

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

    @app.route("/lessons/<int:lid>/edit", methods=["GET", "POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_edit(lid):
        lesson = Lesson.query.get_or_404(lid)
        if request.method == "POST":
            date_str = request.form.get("lesson_date", "")
            time_str = request.form.get("lesson_time", "")
            duration_raw = request.form.get("duration", "")
            note = request.form.get("note", "").strip()
            status = request.form.get("status", LESSON_STATUS_PLANNED)
            try:
                start_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                duration = float(duration_raw)
                if duration <= 0:
                    raise ValueError("duration must be positive")
                if status not in {LESSON_STATUS_PLANNED, LESSON_STATUS_DONE, LESSON_STATUS_CANCELLED}:
                    raise ValueError("bad status")
            except Exception:
                flash("请正确填写排课信息", "error")
                return render_template("edit_lesson.html", lesson=lesson, page_title="编辑排课")

            conflicts = lesson_overlaps_any(start_at, duration, exclude_lid=lesson.id)
            if conflicts and status != LESSON_STATUS_CANCELLED:
                human = []
                for c in conflicts:
                    c_end = c.start_at + timedelta(hours=c.duration_hours)
                    who = c.student.name if c.student else "未知学生"
                    human.append(f"{who}：{c.start_at.strftime('%m/%d %H:%M')}-{c_end.strftime('%H:%M')}")
                flash(f"排课与以下安排冲突：{'; '.join(human)}", "error")
                return render_template("edit_lesson.html", lesson=lesson, page_title="编辑排课")

            lesson.start_at = start_at
            lesson.duration_hours = duration
            lesson.note = note
            lesson.status = status

            if status == LESSON_STATUS_DONE:
                ensure_session_for_lesson(lesson)
            elif status == LESSON_STATUS_CANCELLED:
                remove_session_for_lesson(lesson)
            else:
                remove_session_for_lesson(lesson)
            db.session.commit()
            flash("排课已更新", "ok")
            return redirect(url_for("dashboard", year=start_at.year, month=start_at.month))
        return render_template("edit_lesson.html", lesson=lesson, page_title="编辑排课")

    @app.route("/lessons/<int:lid>/copy", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_copy(lid):
        lesson = Lesson.query.get_or_404(lid)
        mode = request.form.get("repeat_mode", "weekly")
        count = request.form.get("repeat_count", type=int) or 0
        interval_days = request.form.get("repeat_every", type=int) or 7
        if count <= 0:
            flash("请输入正确的复制次数", "error")
            return redirect(url_for("lesson_edit", lid=lid))
        created = skipped = 0
        for i in range(1, count + 1):
            delta_days = 7 * i if mode == "weekly" else interval_days * i
            new_start = lesson.start_at + timedelta(days=delta_days)
            if lesson_overlaps_any(new_start, lesson.duration_hours):
                skipped += 1
                continue
            db.session.add(Lesson(
                student_id=lesson.student_id,
                start_at=new_start,
                duration_hours=lesson.duration_hours,
                note=lesson.note,
                status=LESSON_STATUS_PLANNED,
            ))
            created += 1
        db.session.commit()
        msg = f"已复制 {created} 条排课"
        if skipped:
            msg += f"（有 {skipped} 条因冲突被跳过）"
        flash(msg, "ok")
        return redirect(url_for("lesson_edit", lid=lid))

    @app.route("/lessons/<int:lid>/done", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_done(lid):
        l = Lesson.query.get_or_404(lid)
        y, m = l.start_at.year, l.start_at.month
        if l.status != LESSON_STATUS_DONE:
            ensure_session_for_lesson(l)
            l.status = LESSON_STATUS_DONE
            db.session.commit()
            flash("已标记完成，并记入当日课时", "ok")
        else:
            flash("该排课已完成（跳过重复记账）", "ok")
        return redirect(url_for("dashboard", year=y, month=m))

    @app.route("/lessons/<int:lid>/cancel", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_cancel(lid):
        l = Lesson.query.get_or_404(lid)
        y, m = l.start_at.year, l.start_at.month
        l.status = LESSON_STATUS_CANCELLED
        remove_session_for_lesson(l)
        db.session.commit()
        flash("排课已取消", "ok")
        return redirect(url_for("dashboard", year=y, month=m))

    @app.route("/lessons/<int:lid>/delete", methods=["POST"])
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER)
    def lesson_delete(lid):
        l = Lesson.query.get_or_404(lid)
        y, m = l.start_at.year, l.start_at.month
        remove_session_for_lesson(l)
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

    # ---- 报表统计 ----
    @app.route("/reports")
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def reports():
        scope = request.args.get("scope", "month")
        today = date.today()
        if scope == "day":
            start = request.args.get("date", today.isoformat())
            try:
                start_date = datetime.strptime(start, "%Y-%m-%d").date()
            except Exception:
                start_date = today
            start_range = start_date
            end_range = start_date + timedelta(days=1)
        elif scope == "year":
            y = request.args.get("year", today.year, type=int)
            start_range = date(y, 1, 1)
            end_range = date(y + 1, 1, 1)
        else:
            y, m = _get_year_month()
            start_range, end_range = month_bounds(y, m)
        sessions = (Session.query.join(Student)
                    .filter(Session.date >= start_range, Session.date < end_range)
                    .order_by(Session.date.asc()).all())
        total_hours = sum(s.hours for s in sessions)
        total_income = sum((s.charged_amount if s.charged_amount else s.hours * s.student.hourly_rate) for s in sessions)
        subject_stats = {}
        for s in sessions:
            bucket = subject_stats.setdefault(s.subject or "未分类", {"hours": 0.0, "income": 0.0})
            bucket["hours"] += s.hours
            bucket["income"] += s.charged_amount if s.charged_amount else s.hours * s.student.hourly_rate
        total_count = len(sessions)
        attendance_rate = 0.0
        if total_count:
            present = len([s for s in sessions if not s.absent])
            attendance_rate = present / total_count
        income_by_student = {}
        for s in sessions:
            income_by_student.setdefault(s.student.name, 0.0)
            income_by_student[s.student.name] += s.charged_amount if s.charged_amount else s.hours * s.student.hourly_rate
        payments_sum = (PaymentRecord.query
                        .filter(PaymentRecord.date >= start_range, PaymentRecord.date < end_range)
                        .with_entities(db.func.sum(PaymentRecord.amount)).scalar() or 0.0)
        return render_template(
            "reports.html",
            scope=scope,
            start=start_range,
            end=end_range - timedelta(days=1),
            total_hours=total_hours,
            total_income=total_income,
            subject_stats=subject_stats,
            attendance_rate=attendance_rate,
            income_by_student=income_by_student,
            payments_sum=payments_sum,
            page_title="统计报表",
        )

    @app.route("/reports/export")
    @login_required
    @role_required(ROLE_SUPERADMIN, ROLE_TEACHER, ROLE_ADMIN)
    def export_reports():
        scope = request.args.get("scope", "month")
        today = date.today()
        if scope == "day":
            start_date = request.args.get("date", today.isoformat())
            try:
                start_range = datetime.strptime(start_date, "%Y-%m-%d").date()
            except Exception:
                start_range = today
            end_range = start_range + timedelta(days=1)
        elif scope == "year":
            y = request.args.get("year", today.year, type=int)
            start_range = date(y, 1, 1)
            end_range = date(y + 1, 1, 1)
        else:
            y, m = _get_year_month()
            start_range, end_range = month_bounds(y, m)
        sessions = (Session.query.join(Student)
                    .filter(Session.date >= start_range, Session.date < end_range)
                    .order_by(Session.date.asc()).all())
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = ["学生", "日期", "科目", "课时", "费用", "授课内容", "反馈"]
        writer.writerow(header)
        for s in sessions:
            fee = s.charged_amount if s.charged_amount else s.hours * s.student.hourly_rate
            writer.writerow([
                s.student.name if s.student else "",
                s.date.isoformat(),
                s.subject,
                f"{s.hours:.2f}",
                f"{fee:.2f}",
                s.content,
                s.feedback or "",
            ])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=report.csv"
        return resp
        
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
   
