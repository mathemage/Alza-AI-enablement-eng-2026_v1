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
class Citation:
    url: str = field(repr=False)
    title: str = field(repr=False)
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedReply:
    text: str = field(repr=False)
    html: str = field(repr=False)
    citations: tuple[Citation, ...] = field(repr=False)
    search_entry_point_html: str | None = field(repr=False)
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    provider_latency_ms: int
    total_latency_ms: int


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
    auto_submitted: str | None = field(default=None, repr=False)
    precedence: str | None = field(default=None, repr=False)
    list_id: str | None = field(default=None, repr=False)
    auto_response_suppress: str | None = field(default=None, repr=False)
