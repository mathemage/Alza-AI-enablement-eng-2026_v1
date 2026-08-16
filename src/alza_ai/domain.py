from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Attachment:
    part_id: str
    filename: str = field(repr=False)
    media_family: str
    media_type: str
    disposition: str
    content_id: str | None = field(repr=False)
    size: int
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AttachmentInsight:
    filename: str = field(repr=False)
    media_type: str
    summary: str = field(repr=False)
    extracted_text: str = field(repr=False)
    relevant_facts: tuple[str, ...] = field(repr=False)
    warnings: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class InboundEmail:
    mailbox_key: str
    message_id: str
    thread_id: str
    rfc_message_id: str = field(repr=False)
    subject: str = field(repr=False)
    sender: str = field(repr=False)
    reply_to: str | None = field(repr=False)
    references: tuple[str, ...] = field(repr=False)
    received_at: datetime
    text: str = field(repr=False)
    attachments: tuple[Attachment, ...] = field(repr=False)
    warnings: tuple[str, ...]
