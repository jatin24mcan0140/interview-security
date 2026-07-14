"""
database.py
SQLAlchemy layer — works with SQLite locally (zero setup) and with a free
Postgres instance in production (Neon / Supabase) by just setting DATABASE_URL.
SQLite is fine for one instance, but doesn't handle many concurrent writes well —
which is exactly the situation "general public, unknown scale" will eventually hit.
Postgres handles that safely and both Neon and Supabase have generous free tiers.
"""
import os
import json
from datetime import datetime, timezone

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.environ.get("DATABASE_URL") or \
    f"sqlite:///{os.path.join(os.path.dirname(__file__), 'instance', 'app.db')}"
# Render/Neon/Heroku-style URLs start with postgres:// — SQLAlchemy 2.x wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def now():
    return datetime.now(timezone.utc)


# ============================================================
# Models
# ============================================================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # 'interviewer' or 'candidate'
    email = Column(String)
    created_at = Column(DateTime, default=now)
    photos = relationship("UserPhoto", cascade="all, delete-orphan", backref="user")


class UserPhoto(Base):
    __tablename__ = "user_photos"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    photo_path = Column(String, nullable=False)
    embedding = Column(Text, nullable=False)  # JSON-encoded float list
    created_at = Column(DateTime, default=now)


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    meeting_code = Column(String, unique=True, nullable=False)
    title = Column(String)
    interviewer_id = Column(Integer, ForeignKey("users.id"))
    candidate_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String, default="scheduled")
    created_at = Column(DateTime, default=now)


class LogEntry(Base):
    __tablename__ = "logs"
    id = Column(Integer, primary_key=True)
    meeting_code = Column(String)
    user_id = Column(Integer)
    event_type = Column(String, nullable=False)
    details = Column(Text)
    timestamp = Column(DateTime, default=now)


class PendingApproval(Base):
    __tablename__ = "pending_approvals"
    id = Column(Integer, primary_key=True)
    meeting_code = Column(String)
    claimed_name = Column(String)
    snapshot_path = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=now)


def init_db():
    os.makedirs(os.path.join(os.path.dirname(__file__), "instance"), exist_ok=True)
    Base.metadata.create_all(engine)


# ============================================================
# User + photo helpers
# ============================================================
def add_user(name, role, email=None):
    with SessionLocal() as s:
        u = User(name=name, role=role, email=email)
        s.add(u)
        s.commit()
        return u.id


def add_user_photo(user_id, photo_path, embedding):
    with SessionLocal() as s:
        p = UserPhoto(user_id=user_id, photo_path=photo_path,
                       embedding=json.dumps(list(map(float, embedding))))
        s.add(p)
        s.commit()


def get_all_photo_embeddings():
    with SessionLocal() as s:
        rows = (
            s.query(UserPhoto, User)
            .join(User, User.id == UserPhoto.user_id)
            .all()
        )
        return [{
            "user_id": photo.user_id,
            "name": user.name,
            "role": user.role,
            "photo_path": photo.photo_path,
            "embedding": json.loads(photo.embedding),
        } for photo, user in rows]


def get_all_users():
    with SessionLocal() as s:
        users = s.query(User).order_by(User.created_at.desc()).all()
        result = []
        for u in users:
            photos = s.query(UserPhoto).filter_by(user_id=u.id).all()
            result.append({
                "id": u.id, "name": u.name, "role": u.role, "email": u.email,
                "photos": [{"id": p.id, "photo_path": p.photo_path} for p in photos],
            })
        return result


def delete_user(user_id):
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if u:
            s.delete(u)
            s.commit()


def delete_photo(photo_id):
    with SessionLocal() as s:
        p = s.get(UserPhoto, photo_id)
        if p:
            s.delete(p)
            s.commit()


# ============================================================
# Meeting helpers
# ============================================================
def create_meeting(meeting_code, title, interviewer_id, candidate_id):
    with SessionLocal() as s:
        m = Meeting(meeting_code=meeting_code, title=title,
                     interviewer_id=interviewer_id or None, candidate_id=candidate_id or None)
        s.add(m)
        s.commit()


def get_meeting(meeting_code):
    with SessionLocal() as s:
        m = s.query(Meeting).filter_by(meeting_code=meeting_code).first()
        if not m:
            return None
        return {"id": m.id, "meeting_code": m.meeting_code, "title": m.title,
                "interviewer_id": m.interviewer_id, "candidate_id": m.candidate_id,
                "status": m.status}


def get_all_meetings():
    with SessionLocal() as s:
        rows = s.query(Meeting).order_by(Meeting.created_at.desc()).all()
        result = []
        for m in rows:
            iv = s.get(User, m.interviewer_id) if m.interviewer_id else None
            cd = s.get(User, m.candidate_id) if m.candidate_id else None
            result.append({
                "meeting_code": m.meeting_code, "title": m.title,
                "interviewer_name": iv.name if iv else "—",
                "candidate_name": cd.name if cd else "—",
            })
        return result


def delete_meeting(meeting_code):
    """Deletes a meeting link and cleans up any pending_approvals tied to it,
    so orphaned review requests don't linger in the admin queue afterward."""
    with SessionLocal() as s:
        m = s.query(Meeting).filter_by(meeting_code=meeting_code).first()
        if m:
            s.delete(m)
        s.query(PendingApproval).filter_by(meeting_code=meeting_code).delete()
        s.commit()


# ============================================================
# Logs
# ============================================================
def add_log(meeting_code, user_id, event_type, details=""):
    with SessionLocal() as s:
        s.add(LogEntry(meeting_code=meeting_code, user_id=user_id,
                        event_type=event_type, details=details))
        s.commit()


def get_all_logs():
    with SessionLocal() as s:
        rows = s.query(LogEntry).order_by(LogEntry.timestamp.desc()).limit(500).all()
        result = []
        for l in rows:
            user = s.get(User, l.user_id) if l.user_id else None
            result.append({
                "timestamp": l.timestamp, "meeting_code": l.meeting_code,
                "user_name": user.name if user else None,
                "event_type": l.event_type, "details": l.details,
            })
        return result


# ============================================================
# Manual override / pending approvals
# ============================================================
def add_pending_approval(meeting_code, claimed_name, snapshot_path):
    with SessionLocal() as s:
        p = PendingApproval(meeting_code=meeting_code, claimed_name=claimed_name,
                             snapshot_path=snapshot_path)
        s.add(p)
        s.commit()
        return p.id


def has_pending_for_meeting(meeting_code):
    """True if a review request is already awaiting admin action for this meeting."""
    with SessionLocal() as s:
        return s.query(PendingApproval).filter_by(
            meeting_code=meeting_code, status="pending"
        ).first() is not None


def count_review_requests(meeting_code):
    """Total review requests (any status) ever filed for this meeting — used to
    cap retries at 2 per meeting."""
    with SessionLocal() as s:
        return s.query(PendingApproval).filter_by(meeting_code=meeting_code).count()


def get_pending_approvals():
    with SessionLocal() as s:
        rows = s.query(PendingApproval).filter_by(status="pending").order_by(
            PendingApproval.created_at.desc()).all()
        return [{"id": p.id, "meeting_code": p.meeting_code, "claimed_name": p.claimed_name,
                  "snapshot_path": p.snapshot_path} for p in rows]


def resolve_approval(request_id, status):
    with SessionLocal() as s:
        p = s.get(PendingApproval, request_id)
        if p:
            p.status = status
            s.commit()


def get_approval(request_id):
    with SessionLocal() as s:
        p = s.get(PendingApproval, request_id)
        if not p:
            return None
        return {"id": p.id, "meeting_code": p.meeting_code,
                "claimed_name": p.claimed_name, "status": p.status}
