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
import uuid
import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Конфигурация приложения и базы данных
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

    @property
    def type_label(self) -> str:
        return EVIDENCE_TYPES.get(self.evidence_type, self.evidence_type)


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, ForeignKey("investigations.id"), nullable=False)
    author = Column(String(255), nullable=False, default="Аноним")
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    investigation = relationship("Investigation", back_populates="comments")


Base.metadata.create_all(bind=engine)


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


class CommentCreate(BaseModel):
    author: str = "Аноним"
    content: str


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

app = FastAPI(title=APP_NAME)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def slugify(title: str, db: Session) -> str:
    base = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ\-]+", "-", title.strip().lower())
    base = re.sub(r"-{2,}", "-", base).strip("-") or "delo"
    slug = base
    suffix = 1
    while db.query(Investigation).filter(Investigation.slug == slug).first():
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def common_context(request: Request) -> dict:
    return {
        "request": request,
        "app_name": APP_NAME,
        "app_author": APP_AUTHOR,
        "status_choices": STATUS_CHOICES,
        "category_choices": CATEGORY_CHOICES,
        "node_types": NODE_TYPES,
        "evidence_types": EVIDENCE_TYPES,
    }


# ---------------------------------------------------------------------------
# Страницы (server-rendered, Jinja2)
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    investigations = (
        db.query(Investigation).order_by(Investigation.created_at.desc()).all()
    )
    ctx = common_context(request)
    ctx["investigations"] = investigations
    return templates.TemplateResponse("index.html", ctx)


@app.get("/investigation/{investigation_id}")
def investigation_detail(
    investigation_id: int, request: Request, db: Session = Depends(get_db)
):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    ctx = common_context(request)
    ctx["inv"] = inv
    return templates.TemplateResponse("investigation.html", ctx)


@app.get("/create")
def create_page(request: Request):
    ctx = common_context(request)
    return templates.TemplateResponse("create.html", ctx)


# ---------------------------------------------------------------------------
# API: создание расследования (граф + таймлайн + улики единым JSON)
# ---------------------------------------------------------------------------

@app.post("/api/investigations")
def api_create_investigation(
    payload: InvestigationCreate, db: Session = Depends(get_db)
):
    if payload.status not in STATUS_CHOICES:
        payload.status = "collecting"

    inv = Investigation(
        slug=slugify(payload.title, db),
        title=payload.title.strip(),
        description=payload.description.strip(),
        author=(payload.author.strip() or "Аноним"),
        status=payload.status,
        category=(payload.category.strip() or "Другое"),
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
    author: str = Form("Аноним"),
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    inv = db.query(Investigation).filter(Investigation.id == investigation_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Расследование не найдено")
    if content.strip():
        db.add(
            Comment(
                investigation_id=inv.id,
                author=(author.strip() or "Аноним"),
                content=content.strip(),
            )
        )
        db.commit()
    return RedirectResponse(url=f"/investigation/{investigation_id}#comments", status_code=303)


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
