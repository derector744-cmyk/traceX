"""
traceX — Форум расследований с интерактивным Графом Связей (Obsidian style).
Автор проекта: sck_anonn

Полностью самодостаточное FastAPI-приложение:
- SQLAlchemy ORM поверх SQLite
- REST/JSON API для графа связей (глобального и локального)
- Server-rendered страницы на Jinja2 + TailwindCSS (CDN) + Vis.js Network (CDN)

Запуск (разработка):
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Запуск (прод, см. Dockerfile / tracex.service):
    uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
"""

import os
import re
import io
import uuid
import secrets
import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    text as sa_text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from pydantic import BaseModel, Field
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError


# ---------------------------------------------------------------------------
# Конфигурация приложения и базы данных
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# На Render (и большинстве бесплатных PaaS) диск контейнера эфемерный —
# при каждом передеплое/перезапуске файл SQLite будет создан заново
# и все данные потеряются. Поэтому если задана переменная окружения
# DATABASE_URL (например, бесплатный Postgres от Neon/Supabase/Render),
# используем её — это единственный способ надёжно хранить данные при
# хостинге без постоянного диска. Если DATABASE_URL не задан — работаем
# по-старому на локальном SQLite-файле (удобно для разработки).
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Некоторые провайдеры отдают "postgres://", а SQLAlchemy 2.x требует "postgresql://"
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    DB_DIR = os.environ.get("DB_DIR", BASE_DIR)
    os.makedirs(DB_DIR, exist_ok=True)
    DATABASE_URL = f"sqlite:///{os.path.join(DB_DIR, 'tracex.db')}"
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

APP_NAME = "traceX"
APP_AUTHOR = "sck_anonn"
OWNER_USERNAME = "sckven"          # аккаунт с галочкой "Владелец сайта" и линией поддержки
SYSTEM_USERNAME = "tracex-system"  # служебный бот для авто-уведомлений (подписки на дела)
GLOBAL_CHAT_COOLDOWN_SECONDS = 5

STATUS_CHOICES = {
    "collecting": "Идёт сбор улик",
    "completed": "Завершено",
    "factcheck": "Требуется фактчекинг",
}

CATEGORY_CHOICES = [
    "Коррупция",
    "Экология",
    "Финансы",
    "Политика",
    "Технологии",
    "Общество",
    "Другое",
]

NODE_TYPES = {
    "person": {"label": "Фигурант", "color": "#f97316"},
    "organization": {"label": "Организация", "color": "#3b82f6"},
    "topic": {"label": "Тема", "color": "#22c55e"},
    "evidence": {"label": "Улика", "color": "#eab308"},
    "location": {"label": "Локация", "color": "#ec4899"},
    "other": {"label": "Другое", "color": "#a78bfa"},
}

EVIDENCE_TYPES = {
    "link": "Ссылка",
    "document": "Документ",
    "image": "Изображение",
    "text": "Текстовая заметка",
}


# ---------------------------------------------------------------------------
# ORM-модели
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    bio = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    investigations = relationship("Investigation", back_populates="owner")

    @property
    def is_owner(self) -> bool:
        return self.username == OWNER_USERNAME


class Investigation(Base):
    __tablename__ = "investigations"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=False, default="")
    author = Column(String(255), nullable=False, default="Аноним")
    status = Column(String(50), nullable=False, default="collecting")
    category = Column(String(255), nullable=False, default="Другое")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_private = Column(Boolean, nullable=False, default=False)

    owner = relationship("User", back_populates="investigations")

    nodes = relationship(
        "GraphNode", back_populates="investigation", cascade="all, delete-orphan"
    )
    edges = relationship(
        "GraphEdge", back_populates="investigation", cascade="all, delete-orphan"
    )
    timeline_events = relationship(
        "TimelineEvent",
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="TimelineEvent.event_date",
    )
    evidence_items = relationship(
        "Evidence", back_populates="investigation", cascade="all, delete-orphan"
    )
    comments = relationship(
        "Comment",
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )
    subscribers = relationship(
        "InvestigationSubscriber", back_populates="investigation", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return STATUS_CHOICES.get(self.status, self.status)


class GraphNode(Base):
    """Узел графа связей: фигурант, организация, тема, улика, локация..."""

    __tablename__ = "graph_nodes"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    label = Column(String(500), nullable=False)
    node_type = Column(String(50), nullable=False, default="other")
    description = Column(Text, nullable=False, default="")

    investigation = relationship("Investigation", back_populates="nodes")

    @property
    def type_label(self) -> str:
        return NODE_TYPES.get(self.node_type, NODE_TYPES["other"])["label"]

    @property
    def color(self) -> str:
        return NODE_TYPES.get(self.node_type, NODE_TYPES["other"])["color"]


class GraphEdge(Base):
    """Ребро графа связей: связь между двумя узлами внутри расследования."""

    __tablename__ = "graph_edges"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    source_id = Column(Integer, ForeignKey("graph_nodes.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("graph_nodes.id"), nullable=False)
    label = Column(String(500), nullable=False, default="связан с")

    investigation = relationship("Investigation", back_populates="edges")
    source = relationship("GraphNode", foreign_keys=[source_id])
    target = relationship("GraphNode", foreign_keys=[target_id])


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    event_date = Column(String(50), nullable=False)  # ISO-строка YYYY-MM-DD
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=False, default="")

    investigation = relationship("Investigation", back_populates="timeline_events")


class Evidence(Base):
    __tablename__ = "evidence"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    title = Column(String(500), nullable=False)
    evidence_type = Column(String(50), nullable=False, default="link")
    content = Column(Text, nullable=False, default="")  # URL или текст заметки
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    investigation = relationship("Investigation", back_populates="evidence_items")
    votes = relationship("EvidenceVote", back_populates="evidence", cascade="all, delete-orphan")

    @property
    def type_label(self) -> str:
        return EVIDENCE_TYPES.get(self.evidence_type, self.evidence_type)

    @property
    def verified_count(self) -> int:
        return sum(1 for v in self.votes if v.vote == "verified")

    @property
    def disputed_count(self) -> int:
        return sum(1 for v in self.votes if v.vote == "disputed")


class EvidenceVote(Base):
    """Голос пользователя за достоверность улики (подтверждено/оспаривается)."""

    __tablename__ = "evidence_votes"

    id = Column(Integer, primary_key=True, index=True)
    evidence_id = Column(Integer, ForeignKey("evidence.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vote = Column(String(20), nullable=False)  # "verified" | "disputed"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    evidence = relationship("Evidence", back_populates="votes")
    user = relationship("User")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    author = Column(String(255), nullable=False, default="Аноним")
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    investigation = relationship("Investigation", back_populates="comments")


class InvestigationSubscriber(Base):
    """Подписка пользователя на уведомления о новых комментариях к делу."""

    __tablename__ = "investigation_subscribers"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    investigation = relationship("Investigation", back_populates="subscribers")
    user = relationship("User")


class GlobalMessage(Base):
    """Сообщение в общем чате всего сайта."""

    __tablename__ = "global_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(String(1000), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User")


class AdminLog(Base):
    """Журнал модераторских действий (кто и когда что удалил/изменил)."""

    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(255), nullable=False)
    target = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    admin = relationship("User")


class Message(Base):
    """Личное сообщение между двумя пользователями (чат по нику)."""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_read = Column(Boolean, nullable=False, default=False)

    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


Base.metadata.create_all(bind=engine)


def _table_has_column(conn, table: str, column: str) -> bool:
    is_sqlite = engine.dialect.name == "sqlite"
    if is_sqlite:
        cols = [row[1] for row in conn.execute(sa_text(f"PRAGMA table_info({table})"))]
    else:
        cols = [
            row[0]
            for row in conn.execute(
                sa_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"
                ),
                {"t": table},
            )
        ]
    return column in cols


def _run_light_migrations():
    """Добавляет недостающие колонки/таблицы в уже существующую БД (напр. после апдейта)."""
    with engine.connect() as conn:
        if not _table_has_column(conn, "investigations", "owner_id"):
            conn.execute(sa_text("ALTER TABLE investigations ADD COLUMN owner_id INTEGER"))
            conn.commit()
        if not _table_has_column(conn, "investigations", "is_private"):
            conn.execute(
                sa_text("ALTER TABLE investigations ADD COLUMN is_private BOOLEAN DEFAULT FALSE")
            )
            conn.commit()
        if not _table_has_column(conn, "users", "bio"):
            conn.execute(sa_text("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''"))
            conn.commit()


_run_light_migrations()


# ---------------------------------------------------------------------------
# Аутентификация: хеширование паролей, сессии, seed-админ
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    print(
        "[traceX] ВНИМАНИЕ: переменная окружения SECRET_KEY не задана — "
        "используется одноразовый случайный ключ (сессии будут сбрасываться при рестарте). "
        "Задайте SECRET_KEY в проде."
    )

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def _seed_admin():
    db = SessionLocal()
    try:
        existing_admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        if existing_admin:
            return
        password = ADMIN_PASSWORD or secrets.token_urlsafe(9)
        user = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if user:
            user.is_admin = True
            if ADMIN_PASSWORD:
                user.password_hash = hash_password(ADMIN_PASSWORD)
            db.commit()
            print(f"[traceX] Пользователю '{ADMIN_USERNAME}' выданы права администратора.")
            return
        user = User(
            username=ADMIN_USERNAME,
            email=None,
            password_hash=hash_password(password),
            is_admin=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Другой воркер (при --workers > 1) уже создал этого пользователя
            # ровно в этот же момент — это ожидаемо и безопасно, просто выходим.
            db.rollback()
            return
        print("=" * 70)
        print(f"[traceX] Создан администратор по умолчанию:")
        print(f"         логин:  {ADMIN_USERNAME}")
        if not ADMIN_PASSWORD:
            print(f"         пароль: {password}  (сгенерирован автоматически)")
            print("         Смените пароль после первого входа! Или задайте")
            print("         переменные окружения ADMIN_USERNAME / ADMIN_PASSWORD.")
        else:
            print("         пароль: задан через переменную окружения ADMIN_PASSWORD")
        print("=" * 70)
    finally:
        db.close()


_seed_admin()


def _seed_system_user():
    """Создаёт служебный аккаунт-бота для авто-уведомлений о подписках (без возможности входа)."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == SYSTEM_USERNAME).first()
        if existing:
            return
        user = User(
            username=SYSTEM_USERNAME,
            email=None,
            password_hash=hash_password(secrets.token_urlsafe(32)),  # случайный, вход невозможен
            is_admin=False,
            bio="Системные уведомления traceX.",
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    finally:
        db.close()


_seed_system_user()


def notify_subscribers(db: Session, inv: "Investigation", text: str, exclude_user_id: Optional[int] = None):
    """Отправляет автоматическое сообщение от системного бота всем подписчикам дела."""
    bot = db.query(User).filter(User.username == SYSTEM_USERNAME).first()
    if not bot:
        return
    subs = db.query(InvestigationSubscriber).filter(
        InvestigationSubscriber.investigation_id == inv.id
    ).all()
    for sub in subs:
        if exclude_user_id and sub.user_id == exclude_user_id:
            continue
        db.add(Message(sender_id=bot.id, recipient_id=sub.user_id, content=text[:4000]))
    db.commit()


def log_admin_action(db: Session, admin: Optional[User], action: str, target: str = ""):
    db.add(AdminLog(admin_id=admin.id if admin else None, action=action, target=target))
    db.commit()


# ---------------------------------------------------------------------------
# Pydantic-схемы для API создания расследования
# ---------------------------------------------------------------------------

class NodeIn(BaseModel):
    temp_id: str
    label: str = Field(..., min_length=1, max_length=500)
    node_type: str = "other"
    description: str = ""


class EdgeIn(BaseModel):
    source_temp_id: str
    target_temp_id: str
    label: str = "связан с"


class TimelineIn(BaseModel):
    event_date: str
    title: str = Field(..., min_length=1, max_length=500)
    description: str = ""


class EvidenceIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    evidence_type: str = "link"
    content: str = ""
    description: str = ""


class InvestigationCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=500)
    description: str = ""
    author: str = "Аноним"
    status: str = "collecting"
    category: str = "Другое"
    nodes: List[NodeIn] = []
    edges: List[EdgeIn] = []
    timeline: List[TimelineIn] = []
    evidence: List[EvidenceIn] = []
    is_private: bool = False


class CommentCreate(BaseModel):
    author: str = "Аноним"
    content: str


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", max_age=60 * 60 * 24 * 30)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _linkify_mentions(text: str):
    """Jinja-фильтр: превращает @username в кликабельную ссылку на /u/username.
    Экранирует HTML заранее, поэтому безопасен для вставки как |safe."""
    from markupsafe import escape, Markup
    escaped = str(escape(text or ""))

    def repl(m):
        uname = m.group(1)
        return f'<a href="/u/{uname}" class="text-obsidian-accent2 hover:underline font-medium">@{uname}</a>'

    result = re.sub(r"@([A-Za-zА-Яа-яЁё0-9_\-]{2,32})", repl, escaped)
    return Markup(result)


templates.env.filters["linkify_mentions"] = _linkify_mentions

static_dir = os.path.join(BASE_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Помощники аутентификации
# ---------------------------------------------------------------------------

def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user


def pop_flash(request: Request) -> Optional[dict]:
    return request.session.pop("flash", None)


def set_flash(request: Request, message: str, kind: str = "success"):
    request.session["flash"] = {"message": message, "kind": kind}


def slugify(title: str, db: Session) -> str:
    base = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ\-]+", "-", title.strip().lower())
    base = re.sub(r"-{2,}", "-", base).strip("-") or "delo"
    slug = base
    suffix = 1
    while db.query(Investigation).filter(Investigation.slug == slug).first():
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


# ---------------------------------------------------------------------------
# Фича 9: черновое авто-извлечение сущностей из текста (для конструктора графа)
# ---------------------------------------------------------------------------

_STOPWORDS_RU_START = {
    "Также", "Однако", "Кроме", "После", "Перед", "Если", "Когда", "Потому",
    "Таким", "Более", "Менее", "Первый", "Второй", "Третий", "Этот", "Эта", "Это",
    "Тогда", "Затем", "Далее", "Согласно", "Например",
}


def extract_entity_candidates(text: str) -> List[dict]:
    """Ищет в тексте последовательности слов с заглавной буквы (имена, названия
    организаций и т.п.) и возвращает уникальный список кандидатов на узлы графа.
    Это простая эвристика, а не полноценный NLP — она даёт черновой набор,
    который пользователь может поправить руками."""
    if not text:
        return []
    pattern = r"(?:[A-ZА-ЯЁ][a-zа-яё\-]+(?:\s+[A-ZА-ЯЁ][a-zа-яё\-]+){0,2})"
    seen = {}
    for match in re.finditer(pattern, text):
        phrase = match.group(0).strip()
        first_word = phrase.split()[0]
        if first_word in _STOPWORDS_RU_START:
            continue
        if len(phrase) < 4:
            continue
        key = phrase.lower()
        if key not in seen:
            seen[key] = phrase
    return [{"label": v} for v in list(seen.values())[:40]]


# ---------------------------------------------------------------------------
# Фича 7: экспорт расследования в PDF
# ---------------------------------------------------------------------------

def build_investigation_pdf(inv: "Investigation") -> io.BytesIO:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=inv.title,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1x", parent=styles["Heading1"], textColor=colors.HexColor("#3a2f8f"))
    h2 = ParagraphStyle("h2x", parent=styles["Heading2"], textColor=colors.HexColor("#5b4bc4"))
    body = ParagraphStyle("bodyx", parent=styles["BodyText"], leading=15)
    small = ParagraphStyle("smallx", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)

    story = [
        Paragraph(inv.title, h1),
        Paragraph(
            f"Статус: {inv.status_label} · Категория: {inv.category} · "
            f"Автор: {inv.author} · Дата: {inv.created_at.strftime('%d.%m.%Y')}",
            small,
        ),
        Spacer(1, 8),
        Paragraph(inv.description or "Описание отсутствует.", body),
        Spacer(1, 14),
    ]

    if inv.nodes:
        story.append(Paragraph("Объекты графа связей", h2))
        rows = [["Название", "Тип", "Описание"]]
        for n in inv.nodes:
            rows.append([n.label, n.type_label, n.description or "—"])
        t = Table(rows, colWidths=[130, 90, 220])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#8875ff")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story += [t, Spacer(1, 14)]

    if inv.edges:
        story.append(Paragraph("Связи между объектами", h2))
        by_id = {n.id: n.label for n in inv.nodes}
        rows = [["Откуда", "Тип связи", "Куда"]]
        for e in inv.edges:
            rows.append([by_id.get(e.source_id, "?"), e.label, by_id.get(e.target_id, "?")])
        t = Table(rows, colWidths=[130, 130, 130])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#8875ff")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ]))
        story += [t, Spacer(1, 14)]

    if inv.timeline_events:
        story.append(Paragraph("Таймлайн событий", h2))
        for ev in inv.timeline_events:
            story.append(Paragraph(f"<b>{ev.event_date}</b> — {ev.title}", body))
            if ev.description:
                story.append(Paragraph(ev.description, small))
        story.append(Spacer(1, 14))

    if inv.evidence_items:
        story.append(Paragraph("Улики и доказательства", h2))
        for ev in inv.evidence_items:
            story.append(Paragraph(f"<b>[{ev.type_label}] {ev.title}</b>", body))
            if ev.content:
                story.append(Paragraph(ev.content, small))
            if ev.description:
                story.append(Paragraph(ev.description, small))
            story.append(Paragraph(
                f"✅ Подтверждено: {ev.verified_count}  ·  ⚠️ Оспаривается: {ev.disputed_count}",
                small,
            ))
            story.append(Spacer(1, 6))

    story.append(Spacer(1, 20))
    story.append(Paragraph(f"Сформировано автоматически платформой {APP_NAME}.", small))

    doc.build(story)
    buf.seek(0)
    return buf


def unread_message_count(db: Session, user: Optional[User]) -> int:
    if not user:
        return 0
    return (
        db.query(Message)
        .filter(Message.recipient_id == user.id, Message.is_read == False)  # noqa: E712
        .count()
    )


def common_context(request: Request, db: Optional[Session] = None) -> dict:
    current_user = None
    unread = 0
    if db is not None:
        current_user = get_current_user(request, db)
        unread = unread_message_count(db, current_user)
    return {
        "request": request,
        "app_name": APP_NAME,
        "app_author": APP_AUTHOR,
        "status_choices": STATUS_CHOICES,
        "category_choices": CATEGORY_CHOICES,
        "node_types": NODE_TYPES,
        "evidence_types": EVIDENCE_TYPES,
        "current_user": current_user,
        "flash": pop_flash(request),
        "unread_count": unread,
        "owner_username": OWNER_USERNAME,
        "support_username": OWNER_USERNAME,
    }


# ---------------------------------------------------------------------------
# Страницы (server-rendered, Jinja2)
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    query = db.query(Investigation)
    if current_user and current_user.is_admin:
        pass  # админ видит всё, включая черновики
    elif current_user:
        query = query.filter(
            (Investigation.is_private == False)  # noqa: E712
            | (Investigation.owner_id == current_user.id)
        )
    else:
        query = query.filter(Investigation.is_private == False)  # noqa: E712
    investigations = query.order_by(Investigation.created_at.desc()).all()
    ctx = common_context(request, db)
    ctx["investigations"] = investigations
    return templates.TemplateResponse("index.html", ctx)


@app.get("/investigation/{investigation_id}")
def investigation_detail(
    investigation_id: int, request: Request, db: Session = Depends(get_db)
):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    ctx = common_context(request, db)
    current_user = ctx["current_user"]
    can_view_private = bool(
        current_user and (current_user.is_admin or current_user.id == inv.owner_id)
    )
    if inv.is_private and not can_view_private:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    ctx["inv"] = inv
    ctx["can_delete"] = can_view_private
    ctx["is_subscribed"] = bool(
        current_user
        and db.query(InvestigationSubscriber)
        .filter(
            InvestigationSubscriber.investigation_id == inv.id,
            InvestigationSubscriber.user_id == current_user.id,
        )
        .first()
    )
    return templates.TemplateResponse("investigation.html", ctx)


@app.post("/investigation/{investigation_id}/subscribe")
def toggle_subscribe(investigation_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    existing = (
        db.query(InvestigationSubscriber)
        .filter(
            InvestigationSubscriber.investigation_id == inv.id,
            InvestigationSubscriber.user_id == current_user.id,
        )
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        set_flash(request, "Вы отписались от уведомлений по делу.", "success")
    else:
        db.add(InvestigationSubscriber(investigation_id=inv.id, user_id=current_user.id))
        db.commit()
        set_flash(request, "Вы подписались — будем сообщать о новых комментариях.", "success")
    return RedirectResponse(url=f"/investigation/{investigation_id}", status_code=303)


@app.get("/investigation/{investigation_id}/export.pdf")
def export_investigation_pdf(investigation_id: int, request: Request, db: Session = Depends(get_db)):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    current_user = get_current_user(request, db)
    can_view_private = bool(
        current_user and (current_user.is_admin or current_user.id == inv.owner_id)
    )
    if inv.is_private and not can_view_private:
        raise HTTPException(status_code=404, detail="Расследование не найдено")

    from fastapi.responses import StreamingResponse
    buf = build_investigation_pdf(inv)
    filename = f"{inv.slug}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/extract-entities")
def api_extract_entities(payload: dict):
    """Черновая авто-разметка: находит вероятные имена/организации в тексте
    (последовательности слов с заглавной буквы) и предлагает как узлы графа."""
    text_in = (payload or {}).get("text", "") or ""
    candidates = extract_entity_candidates(text_in)
    return JSONResponse({"candidates": candidates})


@app.get("/create")
def create_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        set_flash(request, "Войдите в аккаунт, чтобы опубликовать расследование.", "error")
        return RedirectResponse(url="/login?next=/create", status_code=303)
    ctx = common_context(request, db)
    return templates.TemplateResponse("create.html", ctx)


# ---------------------------------------------------------------------------
# Страницы: регистрация, вход, выход, профиль
# ---------------------------------------------------------------------------

@app.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/", status_code=303)
    ctx = common_context(request, db)
    ctx["error"] = None
    return templates.TemplateResponse("register.html", ctx)


@app.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    email = email.strip() or None

    def error(msg: str):
        ctx = common_context(request, db)
        ctx["error"] = msg
        ctx["form_username"] = username
        ctx["form_email"] = email or ""
        return templates.TemplateResponse("register.html", ctx, status_code=400)

    if len(username) < 3:
        return error("Имя пользователя должно быть не короче 3 символов.")
    if not re.match(r"^[a-zA-Z0-9а-яА-ЯёЁ_\-\.]+$", username):
        return error("Имя пользователя может содержать только буквы, цифры, _ - .")
    if len(password) < 6:
        return error("Пароль должен быть не короче 6 символов.")
    if password != confirm_password:
        return error("Пароли не совпадают.")
    if db.query(User).filter(User.username == username).first():
        return error("Это имя пользователя уже занято.")
    if email and db.query(User).filter(User.email == email).first():
        return error("Этот email уже зарегистрирован.")

    user = User(username=username, email=email, password_hash=hash_password(password), is_admin=False)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return error("Это имя пользователя или email уже заняты.")
    db.refresh(user)

    request.session["user_id"] = user.id
    set_flash(request, f"Добро пожаловать, {user.username}! Аккаунт создан.", "success")
    return RedirectResponse(url="/", status_code=303)


@app.get("/login")
def login_page(request: Request, db: Session = Depends(get_db), next: str = "/"):
    if get_current_user(request, db):
        return RedirectResponse(url="/", status_code=303)
    ctx = common_context(request, db)
    ctx["error"] = None
    ctx["next"] = next
    return templates.TemplateResponse("login.html", ctx)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    username = username.strip()
    user = db.query(User).filter(User.username == username).first()

    if not user or not verify_password(password, user.password_hash):
        ctx = common_context(request, db)
        ctx["error"] = "Неверное имя пользователя или пароль."
        ctx["next"] = next
        return templates.TemplateResponse("login.html", ctx, status_code=400)

    request.session["user_id"] = user.id
    set_flash(request, f"С возвращением, {user.username}!", "success")
    safe_next = next if next and next.startswith("/") else "/"
    return RedirectResponse(url=safe_next, status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    set_flash(request, "Вы вышли из аккаунта.", "success")
    return RedirectResponse(url="/", status_code=303)


@app.get("/profile")
def profile_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login?next=/profile", status_code=303)
    ctx = common_context(request, db)
    ctx["my_investigations"] = (
        db.query(Investigation)
        .filter(Investigation.owner_id == current_user.id)
        .order_by(Investigation.created_at.desc())
        .all()
    )
    return templates.TemplateResponse("profile.html", ctx)


@app.post("/profile/bio")
def update_bio(request: Request, bio: str = Form(""), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    current_user.bio = bio.strip()[:1000]
    db.commit()
    set_flash(request, "Описание профиля обновлено.", "success")
    return RedirectResponse(url="/profile", status_code=303)


@app.get("/u/{username}")
def public_profile(username: str, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or user.username == SYSTEM_USERNAME:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    ctx = common_context(request, db)
    current_user = ctx["current_user"]
    query = db.query(Investigation).filter(Investigation.owner_id == user.id)
    if not (current_user and (current_user.is_admin or current_user.id == user.id)):
        query = query.filter(Investigation.is_private == False)  # noqa: E712
    ctx["profile_user"] = user
    ctx["public_investigations"] = query.order_by(Investigation.created_at.desc()).all()
    return templates.TemplateResponse("public_profile.html", ctx)


# ---------------------------------------------------------------------------
# Личные сообщения (чат по нику)
# ---------------------------------------------------------------------------

@app.get("/messages")
def messages_list(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login?next=/messages", status_code=303)

    my_messages = (
        db.query(Message)
        .filter(
            (Message.sender_id == current_user.id)
            | (Message.recipient_id == current_user.id)
        )
        .order_by(Message.created_at.desc())
        .all()
    )

    conversations = {}
    for m in my_messages:
        partner = m.recipient if m.sender_id == current_user.id else m.sender
        if not partner:
            continue
        entry = conversations.get(partner.id)
        if entry is None:
            conversations[partner.id] = {
                "partner": partner,
                "last_message": m,
                "unread": 0,
            }
        if m.recipient_id == current_user.id and not m.is_read:
            conversations[partner.id]["unread"] += 1

    conversation_list = sorted(
        conversations.values(), key=lambda c: c["last_message"].created_at, reverse=True
    )

    ctx = common_context(request, db)
    ctx["conversations"] = conversation_list
    return templates.TemplateResponse("messages.html", ctx)


@app.get("/messages/{username}")
def messages_thread(username: str, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url=f"/login?next=/messages/{username}", status_code=303)

    partner = db.query(User).filter(User.username == username).first()
    if not partner:
        set_flash(request, f'Пользователь "{username}" не найден.', "error")
        return RedirectResponse(url="/messages", status_code=303)
    if partner.id == current_user.id:
        set_flash(request, "Нельзя написать самому себе.", "error")
        return RedirectResponse(url="/messages", status_code=303)

    thread = (
        db.query(Message)
        .filter(
            (
                (Message.sender_id == current_user.id)
                & (Message.recipient_id == partner.id)
            )
            | (
                (Message.sender_id == partner.id)
                & (Message.recipient_id == current_user.id)
            )
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    # отмечаем входящие как прочитанные
    changed = False
    for m in thread:
        if m.recipient_id == current_user.id and not m.is_read:
            m.is_read = True
            changed = True
    if changed:
        db.commit()

    ctx = common_context(request, db)
    ctx["partner"] = partner
    ctx["thread"] = thread
    ctx["prefill"] = request.query_params.get("text", "")
    return templates.TemplateResponse("chat.html", ctx)


@app.post("/messages/{username}")
def messages_send(
    username: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")

    partner = db.query(User).filter(User.username == username).first()
    if not partner:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if partner.id == current_user.id:
        raise HTTPException(status_code=400, detail="Нельзя написать самому себе")

    content = content.strip()
    if content:
        db.add(
            Message(
                sender_id=current_user.id,
                recipient_id=partner.id,
                content=content[:4000],
            )
        )
        db.commit()

    return RedirectResponse(url=f"/messages/{username}", status_code=303)


@app.get("/api/messages/unread-count")
def api_unread_count(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    return JSONResponse({"unread": unread_message_count(db, current_user)})


# ---------------------------------------------------------------------------
# Визуализатор графа связей — свободный конструктор точек и стрелок,
# не привязанный к конкретному расследованию
# ---------------------------------------------------------------------------

@app.get("/visualizer")
def visualizer_page(request: Request, db: Session = Depends(get_db)):
    ctx = common_context(request, db)
    return templates.TemplateResponse("visualizer.html", ctx)


# ---------------------------------------------------------------------------
# Общий чат сайта (для всех, с лимитом 1 сообщение / 5 сек на человека)
# ---------------------------------------------------------------------------

@app.get("/chat")
def global_chat_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login?next=/chat", status_code=303)
    ctx = common_context(request, db)
    ctx["chat_messages"] = (
        db.query(GlobalMessage).order_by(GlobalMessage.created_at.desc()).limit(100).all()
    )[::-1]
    return templates.TemplateResponse("global_chat.html", ctx)


@app.get("/api/chat/messages")
def api_chat_messages(after_id: int = 0, db: Session = Depends(get_db)):
    q = db.query(GlobalMessage).order_by(GlobalMessage.id.asc())
    if after_id:
        q = q.filter(GlobalMessage.id > after_id)
    else:
        q = db.query(GlobalMessage).order_by(GlobalMessage.created_at.desc()).limit(100)
        rows = q.all()[::-1]
        return JSONResponse([_serialize_chat_msg(m) for m in rows])
    rows = q.limit(200).all()
    return JSONResponse([_serialize_chat_msg(m) for m in rows])


def _serialize_chat_msg(m: "GlobalMessage") -> dict:
    return {
        "id": m.id,
        "username": m.user.username if m.user else "?",
        "is_owner": bool(m.user and m.user.username == OWNER_USERNAME),
        "is_admin": bool(m.user and m.user.is_admin),
        "content": m.content,
        "created_at": m.created_at.strftime("%H:%M"),
    }


@app.post("/api/chat/messages")
def api_chat_send(request: Request, content: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    last = (
        db.query(GlobalMessage)
        .filter(GlobalMessage.user_id == current_user.id)
        .order_by(GlobalMessage.created_at.desc())
        .first()
    )
    if last:
        elapsed = (datetime.datetime.utcnow() - last.created_at).total_seconds()
        if elapsed < GLOBAL_CHAT_COOLDOWN_SECONDS:
            wait = round(GLOBAL_CHAT_COOLDOWN_SECONDS - elapsed, 1)
            raise HTTPException(status_code=429, detail=f"Не спамьте — подождите {wait} сек.")

    msg = GlobalMessage(user_id=current_user.id, content=content[:1000])
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return JSONResponse(_serialize_chat_msg(msg))


# ---------------------------------------------------------------------------
# Полнотекстовый поиск по платформе
# ---------------------------------------------------------------------------

@app.get("/search")
def search_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    ctx = common_context(request, db)
    current_user = ctx["current_user"]
    q = (q or "").strip()
    inv_results, node_results, evidence_results, comment_results = [], [], [], []

    if q and len(q) >= 2:
        like = f"%{q}%"
        base = db.query(Investigation).filter(
            (Investigation.title.ilike(like)) | (Investigation.description.ilike(like))
        )
        if not (current_user and current_user.is_admin):
            if current_user:
                base = base.filter(
                    (Investigation.is_private == False)  # noqa: E712
                    | (Investigation.owner_id == current_user.id)
                )
            else:
                base = base.filter(Investigation.is_private == False)  # noqa: E712
        inv_results = base.order_by(Investigation.created_at.desc()).limit(25).all()

        node_results = (
            db.query(GraphNode)
            .join(Investigation)
            .filter(GraphNode.label.ilike(like), Investigation.is_private == False)  # noqa: E712
            .limit(25)
            .all()
        )
        evidence_results = (
            db.query(Evidence)
            .join(Investigation)
            .filter(
                (Evidence.title.ilike(like) | Evidence.content.ilike(like)),
                Investigation.is_private == False,  # noqa: E712
            )
            .limit(25)
            .all()
        )
        comment_results = (
            db.query(Comment)
            .join(Investigation)
            .filter(Comment.content.ilike(like), Investigation.is_private == False)  # noqa: E712
            .limit(25)
            .all()
        )

    ctx.update({
        "q": q,
        "inv_results": inv_results,
        "node_results": node_results,
        "evidence_results": evidence_results,
        "comment_results": comment_results,
    })
    return templates.TemplateResponse("search.html", ctx)


# ---------------------------------------------------------------------------
# Резервное копирование БД (фича 10) — доступно админу или по секретному токену
# (для авто-бэкапов через Render Cron Job / curl по расписанию)
# ---------------------------------------------------------------------------

BACKUP_TOKEN = os.environ.get("BACKUP_TOKEN")


@app.get("/admin/backup")
def admin_backup(request: Request, token: str = "", db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    authorized = bool(current_user and current_user.is_admin)
    if not authorized and BACKUP_TOKEN and token == BACKUP_TOKEN:
        authorized = True
    if not authorized:
        raise HTTPException(status_code=403, detail="Требуются права администратора")

    def dump(model, fields):
        return [
            {f: (getattr(row, f).isoformat() if isinstance(getattr(row, f), datetime.datetime) else getattr(row, f))
             for f in fields}
            for row in db.query(model).all()
        ]

    data = {
        "exported_at": datetime.datetime.utcnow().isoformat(),
        "users": dump(User, ["id", "username", "email", "is_admin", "bio", "created_at"]),
        "investigations": dump(Investigation, [
            "id", "slug", "title", "description", "author", "status", "category",
            "created_at", "owner_id", "is_private",
        ]),
        "graph_nodes": dump(GraphNode, ["id", "investigation_id", "label", "node_type", "description"]),
        "graph_edges": dump(GraphEdge, ["id", "investigation_id", "source_id", "target_id", "label"]),
        "timeline_events": dump(TimelineEvent, ["id", "investigation_id", "event_date", "title", "description"]),
        "evidence": dump(Evidence, ["id", "investigation_id", "title", "evidence_type", "content", "description", "created_at"]),
        "comments": dump(Comment, ["id", "investigation_id", "author", "content", "created_at"]),
        "messages": dump(Message, ["id", "sender_id", "recipient_id", "content", "created_at", "is_read"]),
    }
    from fastapi.responses import Response
    body = __import__("json").dumps(data, ensure_ascii=False, indent=2)
    filename = f"tracex-backup-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Удаление расследований (владелец или администратор)
# ---------------------------------------------------------------------------

@app.post("/investigation/{investigation_id}/delete")
def delete_investigation(
    investigation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    if not (current_user.is_admin or current_user.id == inv.owner_id):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    title = inv.title
    was_admin_action = current_user.is_admin and current_user.id != inv.owner_id
    db.delete(inv)
    db.commit()
    if was_admin_action:
        log_admin_action(db, current_user, "Удалил расследование", title)
    set_flash(request, f'Расследование "{title}" удалено.', "success")

    if current_user.is_admin and request.headers.get("referer", "").endswith("/admin"):
        return RedirectResponse(url="/admin", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/comment/{comment_id}/delete")
def delete_comment(comment_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = require_admin(request, db)
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    inv_id = comment.investigation_id
    snippet = comment.content[:80]
    db.delete(comment)
    db.commit()
    log_admin_action(db, current_user, "Удалил комментарий", snippet)
    set_flash(request, "Комментарий удалён.", "success")
    return RedirectResponse(url=f"/investigation/{inv_id}#comments", status_code=303)


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------

@app.get("/admin")
def admin_panel(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse(url="/login?next=/admin", status_code=303)
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")

    ctx = common_context(request, db)
    ctx["investigations"] = (
        db.query(Investigation).order_by(Investigation.created_at.desc()).all()
    )
    ctx["users"] = db.query(User).filter(User.username != SYSTEM_USERNAME).order_by(User.created_at.asc()).all()
    ctx["admin_logs"] = (
        db.query(AdminLog).order_by(AdminLog.created_at.desc()).limit(100).all()
    )
    ctx["stats"] = {
        "investigations": db.query(Investigation).count(),
        "users": db.query(User).count(),
        "comments": db.query(Comment).count(),
        "evidence": db.query(Evidence).count(),
        "admins": db.query(User).filter(User.is_admin == True).count(),  # noqa: E712
    }
    return templates.TemplateResponse("admin.html", ctx)


@app.post("/admin/user/{user_id}/toggle-admin")
def admin_toggle_admin(user_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if target.id == current_user.id and target.is_admin:
        admins_left = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admins_left <= 1:
            set_flash(request, "Нельзя снять права с последнего администратора.", "error")
            return RedirectResponse(url="/admin", status_code=303)

    target.is_admin = not target.is_admin
    db.commit()
    log_admin_action(
        db, current_user,
        "Выдал права администратора" if target.is_admin else "Снял права администратора",
        target.username,
    )
    set_flash(
        request,
        f"Пользователь {target.username} теперь "
        + ("администратор." if target.is_admin else "обычный пользователь."),
        "success",
    )
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/user/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = require_admin(request, db)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if target.id == current_user.id:
        set_flash(request, "Нельзя удалить собственный аккаунт из панели.", "error")
        return RedirectResponse(url="/admin", status_code=303)

    # Расследования пользователя не удаляем — просто отвязываем от аккаунта.
    db.query(Investigation).filter(Investigation.owner_id == target.id).update(
        {Investigation.owner_id: None}
    )
    deleted_username = target.username
    db.delete(target)
    db.commit()
    log_admin_action(db, current_user, "Удалил пользователя", deleted_username)
    set_flash(request, f"Пользователь {target.username} удалён.", "success")
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# API: создание расследования (граф + таймлайн + улики единым JSON)
# ---------------------------------------------------------------------------

@app.post("/api/investigations")
def api_create_investigation(
    payload: InvestigationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт, чтобы опубликовать расследование")

    if payload.status not in STATUS_CHOICES:
        payload.status = "collecting"

    inv = Investigation(
        slug=slugify(payload.title, db),
        title=payload.title.strip(),
        description=payload.description.strip(),
        author=(payload.author.strip() or current_user.username),
        status=payload.status,
        category=(payload.category.strip() or "Другое"),
        owner_id=current_user.id,
        is_private=bool(payload.is_private),
    )
    db.add(inv)
    db.flush()  # получить inv.id до commit

    temp_id_to_node_id = {}
    for node_in in payload.nodes:
        node_type = node_in.node_type if node_in.node_type in NODE_TYPES else "other"
        node = GraphNode(
            investigation_id=inv.id,
            label=node_in.label.strip(),
            node_type=node_type,
            description=node_in.description.strip(),
        )
        db.add(node)
        db.flush()
        temp_id_to_node_id[node_in.temp_id] = node.id

    for edge_in in payload.edges:
        src = temp_id_to_node_id.get(edge_in.source_temp_id)
        tgt = temp_id_to_node_id.get(edge_in.target_temp_id)
        if src is None or tgt is None:
            continue
        edge = GraphEdge(
            investigation_id=inv.id,
            source_id=src,
            target_id=tgt,
            label=(edge_in.label.strip() or "связан с"),
        )
        db.add(edge)

    for t in payload.timeline:
        db.add(
            TimelineEvent(
                investigation_id=inv.id,
                event_date=t.event_date.strip(),
                title=t.title.strip(),
                description=t.description.strip(),
            )
        )

    for e in payload.evidence:
        ev_type = e.evidence_type if e.evidence_type in EVIDENCE_TYPES else "link"
        db.add(
            Evidence(
                investigation_id=inv.id,
                title=e.title.strip(),
                evidence_type=ev_type,
                content=e.content.strip(),
                description=e.description.strip(),
            )
        )

    db.commit()
    db.refresh(inv)

    return JSONResponse({"id": inv.id, "slug": inv.slug, "url": f"/investigation/{inv.id}"})


# ---------------------------------------------------------------------------
# API: комментарии и улики (пост-модерация форума)
# ---------------------------------------------------------------------------

@app.post("/investigation/{investigation_id}/comments")
def add_comment(
    investigation_id: int,
    request: Request,
    author: str = Form("Аноним"),
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    current_user = get_current_user(request, db)
    if content.strip():
        final_author = author.strip() or "Аноним"
        db.add(
            Comment(
                investigation_id=inv.id,
                author=final_author,
                content=content.strip(),
            )
        )
        db.commit()
        notify_subscribers(
            db, inv,
            f'Новый комментарий от {final_author} к делу «{inv.title}»: {content.strip()[:200]}',
            exclude_user_id=current_user.id if current_user else None,
        )
    return RedirectResponse(url=f"/investigation/{investigation_id}#comments", status_code=303)


# ---------------------------------------------------------------------------
# Голосование за достоверность улики
# ---------------------------------------------------------------------------

@app.post("/evidence/{evidence_id}/vote")
def vote_evidence(
    evidence_id: int,
    request: Request,
    vote: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = get_current_user(request, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    ev = db.query(Evidence).filter(Evidence.id == evidence_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Улика не найдена")
    if vote not in ("verified", "disputed"):
        raise HTTPException(status_code=400, detail="Некорректный голос")

    existing = (
        db.query(EvidenceVote)
        .filter(EvidenceVote.evidence_id == ev.id, EvidenceVote.user_id == current_user.id)
        .first()
    )
    if existing:
        if existing.vote == vote:
            db.delete(existing)  # повторный клик — отмена голоса
        else:
            existing.vote = vote
    else:
        db.add(EvidenceVote(evidence_id=ev.id, user_id=current_user.id, vote=vote))
    db.commit()
    return RedirectResponse(url=f"/investigation/{ev.investigation_id}#evidence", status_code=303)


@app.post("/investigation/{investigation_id}/evidence")
def add_evidence(
    investigation_id: int,
    title: str = Form(...),
    evidence_type: str = Form("link"),
    content: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    if evidence_type not in EVIDENCE_TYPES:
        evidence_type = "link"
    if title.strip():
        db.add(
            Evidence(
                investigation_id=inv.id,
                title=title.strip(),
                evidence_type=evidence_type,
                content=content.strip(),
                description=description.strip(),
            )
        )
        db.commit()
    return RedirectResponse(url=f"/investigation/{investigation_id}#evidence", status_code=303)


# ---------------------------------------------------------------------------
# API: граф связей (глобальный и локальный)
# ---------------------------------------------------------------------------

@app.get("/api/graph")
def api_global_graph(
    status: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Investigation)
    if status and status in STATUS_CHOICES:
        query = query.filter(Investigation.status == status)
    if category and category != "all":
        query = query.filter(Investigation.category == category)
    investigations = query.all()

    nodes = []
    edges = []

    for inv in investigations:
        case_node_id = f"case-{inv.id}"
        nodes.append(
            {
                "id": case_node_id,
                "label": inv.title,
                "group": "investigation",
                "color": "#8875ff",
                "shape": "diamond",
                "size": 28,
                "title": f"{inv.title}\nСтатус: {inv.status_label}\nКатегория: {inv.category}",
                "url": f"/investigation/{inv.id}",
            }
        )

        for node in inv.nodes:
            gnode_id = f"node-{node.id}"
            nodes.append(
                {
                    "id": gnode_id,
                    "label": node.label,
                    "group": node.node_type,
                    "color": node.color,
                    "title": f"{node.label} ({node.type_label})\n{node.description}",
                    "url": f"/investigation/{inv.id}",
                }
            )
            edges.append(
                {
                    "from": case_node_id,
                    "to": gnode_id,
                    "label": "",
                    "dashes": True,
                    "color": "#4b5563",
                }
            )

        for edge in inv.edges:
            edges.append(
                {
                    "from": f"node-{edge.source_id}",
                    "to": f"node-{edge.target_id}",
                    "label": edge.label,
                    "color": "#9ca3af",
                }
            )

    return JSONResponse({"nodes": nodes, "edges": edges})


@app.get("/api/investigation/{investigation_id}/graph")
def api_local_graph(investigation_id: int, db: Session = Depends(get_db)):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")

    case_node_id = f"case-{inv.id}"
    nodes = [
        {
            "id": case_node_id,
            "label": inv.title,
            "group": "investigation",
            "color": "#8875ff",
            "shape": "diamond",
            "size": 30,
            "title": inv.title,
        }
    ]
    edges = []

    for node in inv.nodes:
        gnode_id = f"node-{node.id}"
        nodes.append(
            {
                "id": gnode_id,
                "label": node.label,
                "group": node.node_type,
                "color": node.color,
                "title": f"{node.label} ({node.type_label})\n{node.description}",
            }
        )
        edges.append(
            {
                "from": case_node_id,
                "to": gnode_id,
                "label": "",
                "dashes": True,
                "color": "#4b5563",
            }
        )

    for edge in inv.edges:
        edges.append(
            {
                "from": f"node-{edge.source_id}",
                "to": f"node-{edge.target_id}",
                "label": edge.label,
                "color": "#9ca3af",
            }
        )

    return JSONResponse({"nodes": nodes, "edges": edges})


@app.get("/api/investigations")
def api_list_investigations(db: Session = Depends(get_db)):
    investigations = (
        db.query(Investigation).order_by(Investigation.created_at.desc()).all()
    )
    return JSONResponse(
        [
            {
                "id": inv.id,
                "title": inv.title,
                "description": inv.description,
                "author": inv.author,
                "status": inv.status,
                "status_label": inv.status_label,
                "category": inv.category,
                "created_at": inv.created_at.strftime("%Y-%m-%d"),
                "url": f"/investigation/{inv.id}",
            }
            for inv in investigations
        ]
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok", "app": APP_NAME}
