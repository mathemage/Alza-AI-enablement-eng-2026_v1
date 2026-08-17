import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, cast, runtime_checkable
from uuid import uuid4

import google.cloud.storage as storage  # type: ignore[import-untyped]  # noqa: PLR0402
from google import genai
from google.genai import types

from alza_ai.domain import Attachment, AttachmentInsight

REGION = "europe-west3"
DEFAULT_MODEL = "gemini-3.6-flash"
ANALYSIS_TIMEOUT_SECONDS = 30.0
CLEANUP_TIMEOUT_SECONDS = 5.0
CLEANUP_WARNING = "attachment_cleanup_failed"

_SUMMARY_LIMIT = 2_000
_EXTRACTED_TEXT_LIMIT = 16_000
_FACT_LIMIT = 500
_FACT_COUNT_LIMIT = 20
_WARNING_LIMIT = 500
_WARNING_COUNT_LIMIT = 10
_REQUIRED_OUTPUT_FIELDS = (
    "summary",
    "extracted_text",
    "relevant_facts",
    "warnings",
)

_ANALYSIS_PROMPT = (
    "Analyze this single attachment for an email reply. Return a concise summary, "
    "all useful extracted text or an audio transcript, relevant facts, and warnings."
)
_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "extracted_text": {"type": "string"},
        "relevant_facts": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _FACT_COUNT_LIMIT,
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _WARNING_COUNT_LIMIT,
        },
    },
    "required": list(_REQUIRED_OUTPUT_FIELDS),
    "additionalProperties": False,
}

logger = logging.getLogger(__name__)


class AttachmentAnalysisError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GeminiModelError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GeminiAttachmentResult:
    summary: str = field(repr=False)
    extracted_text: str = field(repr=False)
    relevant_facts: tuple[str, ...] = field(repr=False)
    warnings: tuple[str, ...] = field(repr=False)


@runtime_checkable
class ScratchStorage(Protocol):
    region: str
    bucket_name: str

    async def stage(self, *, object_name: str, data: bytes, media_type: str) -> str: ...

    async def delete(self, *, object_name: str) -> None: ...


@runtime_checkable
class GeminiMultimodalModelPort(Protocol):
    async def analyze(
        self, *, gcs_uri: str, media_type: str
    ) -> GeminiAttachmentResult: ...


class AttachmentAnalyzer:
    def __init__(
        self,
        scratch_storage: ScratchStorage,
        model: GeminiMultimodalModelPort,
        *,
        analysis_timeout_seconds: float = ANALYSIS_TIMEOUT_SECONDS,
        cleanup_timeout_seconds: float = CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        if scratch_storage.region != REGION:
            raise ValueError("attachment_storage_region_invalid")
        self._scratch_storage = scratch_storage
        self._model = model
        self._analysis_timeout_seconds = analysis_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds

    async def analyze(
        self, attachments: Sequence[Attachment]
    ) -> tuple[AttachmentInsight, ...]:
        semaphore = asyncio.Semaphore(2)
        tasks = [
            asyncio.create_task(self._run_job(semaphore, attachment))
            for attachment in attachments
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        for result in results:
            if isinstance(result, BaseException):
                raise result
        return tuple(cast(AttachmentInsight, result) for result in results)

    async def _run_job(
        self,
        semaphore: asyncio.Semaphore,
        attachment: Attachment,
    ) -> AttachmentInsight:
        async with semaphore:
            return await self._analyze_one(attachment)

    async def _analyze_one(self, attachment: Attachment) -> AttachmentInsight:
        object_name = uuid4().hex
        phase = "upload"
        cleanup_failed = False
        try:
            try:
                async with asyncio.timeout(self._analysis_timeout_seconds):
                    gcs_uri = await self._scratch_storage.stage(
                        object_name=object_name,
                        data=attachment.data,
                        media_type=attachment.media_type,
                    )
                    phase = "model"
                    result = await self._model.analyze(
                        gcs_uri=gcs_uri,
                        media_type=attachment.media_type,
                    )
                    insight = _normalize_insight(attachment, result)
            except TimeoutError:
                raise AttachmentAnalysisError("attachment_analysis_timeout") from None
            except GeminiModelError as error:
                code = (
                    error.code
                    if error.code
                    in {
                        "attachment_model_failed",
                        "attachment_model_invalid_response",
                    }
                    else "attachment_model_failed"
                )
                raise AttachmentAnalysisError(code) from None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - sanitize adapter boundary failures
                code = (
                    "attachment_upload_failed"
                    if phase == "upload"
                    else "attachment_model_failed"
                )
                raise AttachmentAnalysisError(code) from None
        finally:
            cleanup_failed = await self._cleanup(object_name)

        if cleanup_failed:
            warnings = (
                CLEANUP_WARNING,
                *(
                    warning
                    for warning in insight.warnings
                    if warning != CLEANUP_WARNING
                ),
            )[:_WARNING_COUNT_LIMIT]
            return replace(insight, warnings=warnings)
        return insight

    async def _cleanup(self, object_name: str) -> bool:
        async def delete() -> None:
            async with asyncio.timeout(self._cleanup_timeout_seconds):
                await self._scratch_storage.delete(object_name=object_name)

        cleanup_task = asyncio.create_task(delete())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                try:
                    await cleanup_task
                except Exception:  # noqa: BLE001 - cleanup cannot mask cancellation
                    logger.warning(CLEANUP_WARNING)
                raise
            logger.warning(CLEANUP_WARNING)
            return True
        except Exception:  # noqa: BLE001 - cleanup adapters expose no shared error
            logger.warning(CLEANUP_WARNING)
            return True
        return False


def _normalize_insight(
    attachment: Attachment,
    result: GeminiAttachmentResult,
) -> AttachmentInsight:
    return AttachmentInsight(
        filename=attachment.filename,
        media_type=attachment.media_type,
        summary=result.summary.strip()[:_SUMMARY_LIMIT],
        extracted_text=result.extracted_text.strip()[:_EXTRACTED_TEXT_LIMIT],
        relevant_facts=_bounded_items(
            result.relevant_facts,
            item_limit=_FACT_LIMIT,
            count_limit=_FACT_COUNT_LIMIT,
        ),
        warnings=_bounded_items(
            result.warnings,
            item_limit=_WARNING_LIMIT,
            count_limit=_WARNING_COUNT_LIMIT,
        ),
    )


def _bounded_items(
    values: Sequence[str],
    *,
    item_limit: int,
    count_limit: int,
) -> tuple[str, ...]:
    bounded: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized:
            bounded.append(normalized[:item_limit])
        if len(bounded) == count_limit:
            break
    return tuple(bounded)


class _Blob(Protocol):
    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        timeout: float,
    ) -> None: ...

    def delete(self, *, timeout: float) -> None: ...


class _Bucket(Protocol):
    def blob(self, object_name: str) -> _Blob: ...


class _StorageClient(Protocol):
    def bucket(self, bucket_name: str) -> _Bucket: ...


class CloudStorageScratchStorage:
    region = REGION

    def __init__(
        self,
        *,
        bucket_name: str,
        client: object | None = None,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self.bucket_name = bucket_name
        storage_client = cast(
            _StorageClient,
            client if client is not None else storage.Client(),
        )
        self._bucket = storage_client.bucket(bucket_name)
        self._request_timeout_seconds = request_timeout_seconds

    async def stage(self, *, object_name: str, data: bytes, media_type: str) -> str:
        blob = self._bucket.blob(object_name)
        await asyncio.to_thread(
            blob.upload_from_string,
            data,
            content_type=media_type,
            timeout=self._request_timeout_seconds,
        )
        return f"gs://{self.bucket_name}/{object_name}"

    async def delete(self, *, object_name: str) -> None:
        blob = self._bucket.blob(object_name)
        await asyncio.to_thread(
            blob.delete,
            timeout=self._request_timeout_seconds,
        )


class _GeminiResponse(Protocol):
    text: str | None


class _GeminiModels(Protocol):
    async def generate_content(self, **kwargs: object) -> _GeminiResponse: ...


class _GeminiAio(Protocol):
    models: _GeminiModels


class _GeminiClient(Protocol):
    aio: _GeminiAio


class _GeminiClientFactory(Protocol):
    def __call__(self, **kwargs: object) -> object: ...


class GeminiMultimodalModel:
    def __init__(
        self,
        *,
        project_id: str,
        model: str = DEFAULT_MODEL,
        request_timeout_seconds: float = ANALYSIS_TIMEOUT_SECONDS,
        client_factory: object = genai.Client,
    ) -> None:
        http_options = types.HttpOptions(
            api_version="v1",
            timeout=int(request_timeout_seconds * 1_000),
        )
        factory = cast(_GeminiClientFactory, client_factory)
        self._client = cast(
            _GeminiClient,
            factory(
                vertexai=True,
                project=project_id,
                location="global",
                http_options=http_options,
            ),
        )
        self._model = model

    async def analyze(self, *, gcs_uri: str, media_type: str) -> GeminiAttachmentResult:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=[
                    _ANALYSIS_PROMPT,
                    types.Part.from_uri(
                        file_uri=gcs_uri,
                        mime_type=media_type,
                    ),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=_RESPONSE_SCHEMA,
                    max_output_tokens=4_096,
                    temperature=0,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sanitize provider SDK failures
            raise GeminiModelError("attachment_model_failed") from None

        try:
            return _parse_model_response(response.text)
        except KeyError, TypeError, ValueError:
            raise GeminiModelError("attachment_model_invalid_response") from None


def _parse_model_response(response_text: str | None) -> GeminiAttachmentResult:
    if response_text is None:
        raise ValueError
    value = json.loads(response_text)
    if not isinstance(value, dict) or set(value) != set(_REQUIRED_OUTPUT_FIELDS):
        raise ValueError

    summary = value["summary"]
    extracted_text = value["extracted_text"]
    relevant_facts = value["relevant_facts"]
    warnings = value["warnings"]
    if not isinstance(summary, str) or not isinstance(extracted_text, str):
        raise TypeError
    if not _is_string_list(relevant_facts) or not _is_string_list(warnings):
        raise TypeError
    return GeminiAttachmentResult(
        summary=summary,
        extracted_text=extracted_text,
        relevant_facts=tuple(relevant_facts),
        warnings=tuple(warnings),
    )


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
