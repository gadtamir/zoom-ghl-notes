import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class JobStatus(str, enum.Enum):
    received = "received"
    converted = "converted"
    transcribed = "transcribed"
    summarized = "summarized"
    matched = "matched"
    completed = "completed"
    unmatched = "unmatched"
    failed = "failed"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    api_key_prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    jobs: Mapped[list["Job"]] = relationship("Job", back_populates="employee")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    employee_id: Mapped[str] = mapped_column(String(36), ForeignKey("employees.id"), nullable=False, index=True)
    employee_name: Mapped[str] = mapped_column(String(120), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    meeting_topic: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meeting_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20),
        default=JobStatus.received,
        nullable=False,
        index=True,
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ghl_contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ghl_note_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee: Mapped[Employee] = relationship("Employee", back_populates="jobs")


class CallJobStatus(str, enum.Enum):
    received = "received"
    skipped = "skipped"           # didn't pass duration filter
    downloaded = "downloaded"
    transcribed = "transcribed"
    summarized = "summarized"
    completed = "completed"        # note created in GHL
    failed = "failed"


class CallJob(Base):
    """One GHL phone-call recording we've picked up and processed."""
    __tablename__ = "call_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ghl_message_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    ghl_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ghl_contact_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ghl_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ghl_user_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    from_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    call_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[CallJobStatus] = mapped_column(
        Enum(CallJobStatus, native_enum=False, length=20),
        default=CallJobStatus.received,
        nullable=False,
        index=True,
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ghl_note_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ZoomMeetingStatus(str, enum.Enum):
    received = "received"
    skipped = "skipped"            # too short, or no external customer on the call
    downloaded = "downloaded"
    transcribed = "transcribed"
    summarized = "summarized"
    completed = "completed"        # note created in GHL
    failed = "failed"


class ZoomMeeting(Base):
    """One Zoom cloud recording pulled in from the `recording.completed` webhook.

    Separate from `Job` (the desktop-uploader flow) on purpose: a cloud recording
    has no uploading employee, so `Job.employee_id` has nothing to point at.
    `zoom_meeting_uuid` is unique — it's what makes redelivered webhooks a no-op.
    """

    __tablename__ = "zoom_meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    zoom_meeting_uuid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    zoom_meeting_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(500), nullable=True)
    host_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    host_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[ZoomMeetingStatus] = mapped_column(
        Enum(ZoomMeetingStatus, native_enum=False, length=20),
        default=ZoomMeetingStatus.received,
        nullable=False,
        index=True,
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which participant address resolved to the GHL contact — useful when a
    # meeting had several externals and we need to explain the choice.
    matched_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ghl_contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ghl_note_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set once the cloud recording has been removed from Zoom to save storage.
    recording_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
