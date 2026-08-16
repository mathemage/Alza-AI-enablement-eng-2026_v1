import asyncio
import json
import logging
import re
from dataclasses import FrozenInstanceError, dataclass
from typing import cast

import pytest
from google.genai import types

from alza_ai.attachments import (
    CLEANUP_WARNING,
    AttachmentAnalysisError,
    AttachmentAnalyzer,
    CloudStorageScratchStorage,
    GeminiAttachmentResult,
    GeminiModelError,
    GeminiMultimodalModel,
    GeminiMultimodalModelPort,
    ScratchStorage,
)
from alza_ai.domain import Attachment, AttachmentInsight

PRIVATE_MARKER = "private-attachment-marker"
SCRATCH_BUCKET = "example-project-alza-ai-scratch"
REGION = "europe-west3"


@dataclass(frozen=True, slots=True)
class UploadCall:
    object_name: str
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class ModelCall:
    gcs_uri: str
    media_type: str


class FakeScratchStorage:
    def __init__(
        self,
        *,
        region: str = REGION,
        fail_upload_types: frozenset[str] = frozenset(),
        fail_delete: bool = False,
        block_delete: bool = False,
    ) -> None:
        self.region = region
        self.bucket_name = SCRATCH_BUCKET
        self.fail_upload_types = fail_upload_types
        self.fail_delete = fail_delete
        self.block_delete = block_delete
        self.uploads: list[UploadCall] = []
        self.deletes: list[str] = []
        self.objects: dict[str, bytes] = {}

    async def stage(self, *, object_name: str, data: bytes, media_type: str) -> str:
        self.uploads.append(UploadCall(object_name, data, media_type))
        self.objects[object_name] = data
        if media_type in self.fail_upload_types:
            raise RuntimeError(f"upload failed: {PRIVATE_MARKER}")
        return f"gs://{self.bucket_name}/{object_name}"

    async def delete(self, *, object_name: str) -> None:
        self.deletes.append(object_name)
        if self.block_delete:
            await asyncio.Event().wait()
        if self.fail_delete:
            raise RuntimeError(f"delete failed: {PRIVATE_MARKER}")
        self.objects.pop(object_name, None)


class FakeGeminiModel:
    def __init__(
        self,
        *,
        result: GeminiAttachmentResult | None = None,
        fail_media_types: frozenset[str] = frozenset(),
        coordinate_concurrency: bool = False,
        block: bool = False,
    ) -> None:
        self.result = result or GeminiAttachmentResult(
            summary="Summary",
            extracted_text="Extracted text or transcript",
            relevant_facts=("Relevant fact",),
            warnings=("Provider warning",),
        )
        self.fail_media_types = fail_media_types
        self.coordinate_concurrency = coordinate_concurrency
        self.block = block
        self.calls: list[ModelCall] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, *, gcs_uri: str, media_type: str) -> GeminiAttachmentResult:
        self.calls.append(ModelCall(gcs_uri, media_type))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.coordinate_concurrency:
                if self.active == 2:
                    self.release.set()
                await self.release.wait()
                await asyncio.sleep(0)
            if self.block:
                await asyncio.Event().wait()
            if media_type in self.fail_media_types:
                raise RuntimeError(f"model failed: {PRIVATE_MARKER}")
            return self.result
        finally:
            self.active -= 1


SUPPORTED_ATTACHMENTS = (
    ("application/pdf", "document", "pdf", b"%PDF-1.7\nfixture"),
    ("audio/mpeg", "audio", "mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00fixture"),
    ("audio/wav", "audio", "wav", b"RIFF\x14\x00\x00\x00WAVEfixture"),
    ("image/jpeg", "image", "jpg", b"\xff\xd8\xff\xe0fixture"),
    ("image/png", "image", "png", b"\x89PNG\r\n\x1a\nfixture"),
)


def make_attachment(
    index: int = 0,
    *,
    media_type: str = "application/pdf",
    media_family: str = "document",
    extension: str = "pdf",
    data: bytes = b"%PDF-1.7\nfixture",
) -> Attachment:
    return Attachment(
        part_id=str(index),
        filename=f"{PRIVATE_MARKER}-{index}.{extension}",
        media_family=media_family,
        media_type=media_type,
        disposition="attachment",
        content_id=None,
        size=len(data),
        data=data,
    )


def run_analysis(
    analyzer: AttachmentAnalyzer, attachments: tuple[Attachment, ...]
) -> tuple[AttachmentInsight, ...]:
    return asyncio.run(analyzer.analyze(attachments))


def test_port_01_fake_storage_and_model_adapters_satisfy_their_contracts() -> None:
    storage = FakeScratchStorage()
    model = FakeGeminiModel()

    assert isinstance(storage, ScratchStorage)
    assert isinstance(model, GeminiMultimodalModelPort)

    async def exercise() -> GeminiAttachmentResult:
        uri = await storage.stage(
            object_name="0" * 32,
            data=b"contract-bytes",
            media_type="application/pdf",
        )
        result = await model.analyze(
            gcs_uri=uri,
            media_type="application/pdf",
        )
        await storage.delete(object_name="0" * 32)
        return result

    assert asyncio.run(exercise()) == model.result
    assert storage.objects == {}
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("media_type", "media_family", "extension", "data"),
    SUPPORTED_ATTACHMENTS,
)
def test_att_02_each_supported_type_is_staged_and_sent_once_to_gemini(
    media_type: str,
    media_family: str,
    extension: str,
    data: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attachment = make_attachment(
        media_type=media_type,
        media_family=media_family,
        extension=extension,
        data=data,
    )
    storage = FakeScratchStorage()
    model = FakeGeminiModel()

    insights = run_analysis(AttachmentAnalyzer(storage, model), (attachment,))

    assert insights == (
        AttachmentInsight(
            filename=attachment.filename,
            media_type=media_type,
            summary="Summary",
            extracted_text="Extracted text or transcript",
            relevant_facts=("Relevant fact",),
            warnings=("Provider warning",),
        ),
    )
    assert len(storage.uploads) == 1
    upload = storage.uploads[0]
    assert upload.data == data
    assert upload.media_type == media_type
    assert re.fullmatch(r"[0-9a-f]{32}", upload.object_name)
    assert PRIVATE_MARKER not in upload.object_name
    assert model.calls == [
        ModelCall(
            f"gs://{SCRATCH_BUCKET}/{upload.object_name}",
            media_type,
        )
    ]
    assert storage.deletes == [upload.object_name]
    assert storage.objects == {}
    assert data not in repr(model.calls).encode()
    assert caplog.records == []


def test_att_02_five_attachments_preserve_order_and_never_exceed_concurrency_two() -> (
    None
):
    attachments = tuple(make_attachment(index) for index in range(5))
    storage = FakeScratchStorage()
    model = FakeGeminiModel(coordinate_concurrency=True)

    insights = run_analysis(AttachmentAnalyzer(storage, model), attachments)

    assert tuple(insight.filename for insight in insights) == tuple(
        attachment.filename for attachment in attachments
    )
    assert model.max_active == 2
    assert len(model.calls) == len(attachments)
    assert len(storage.uploads) == len(attachments)
    assert len(storage.deletes) == len(attachments)
    assert len({call.object_name for call in storage.uploads}) == len(attachments)
    assert storage.objects == {}


def test_domain_01_insight_output_is_frozen_trimmed_bounded_and_repr_safe() -> None:
    model = FakeGeminiModel(
        result=GeminiAttachmentResult(
            summary="  " + "s" * 2_100 + "  ",
            extracted_text="  " + "e" * 16_100 + "  ",
            relevant_facts=(
                "",
                "   ",
                *("  " + f"fact-{index}-" + "f" * 510 + "  " for index in range(22)),
            ),
            warnings=tuple(
                "  " + f"warning-{index}-" + "w" * 510 + "  " for index in range(12)
            ),
        )
    )
    attachment = make_attachment()

    insight = run_analysis(
        AttachmentAnalyzer(FakeScratchStorage(), model),
        (attachment,),
    )[0]

    assert insight.filename == attachment.filename
    assert insight.media_type == attachment.media_type
    assert insight.summary == "s" * 2_000
    assert insight.extracted_text == "e" * 16_000
    assert len(insight.relevant_facts) == 20
    assert all(0 < len(fact) <= 500 for fact in insight.relevant_facts)
    assert len(insight.warnings) == 10
    assert all(0 < len(warning) <= 500 for warning in insight.warnings)
    with pytest.raises(FrozenInstanceError):
        setattr(insight, "summary", "changed")  # noqa: B010
    representation = repr(insight)
    assert PRIVATE_MARKER not in representation
    assert "s" * 100 not in representation
    assert "e" * 100 not in representation
    assert "fact-0" not in representation
    assert "warning-0" not in representation


def test_att_03_partial_model_failure_finishes_siblings_and_cleans_every_object() -> (
    None
):
    attachments = (
        make_attachment(0),
        make_attachment(
            1,
            media_type="image/jpeg",
            media_family="image",
            extension="jpg",
            data=b"\xff\xd8\xff\xe0fixture",
        ),
        make_attachment(
            2,
            media_type="image/png",
            media_family="image",
            extension="png",
            data=b"\x89PNG\r\n\x1a\nfixture",
        ),
    )
    storage = FakeScratchStorage()
    model = FakeGeminiModel(fail_media_types=frozenset({"image/jpeg"}))

    with pytest.raises(
        AttachmentAnalysisError,
        match="^attachment_model_failed$",
    ) as raised:
        run_analysis(AttachmentAnalyzer(storage, model), attachments)

    assert raised.value.code == "attachment_model_failed"
    assert str(raised.value) == "attachment_model_failed"
    assert PRIVATE_MARKER not in str(raised.value)
    assert len(model.calls) == 3
    assert len(storage.deletes) == 3
    assert storage.objects == {}


def test_att_03_upload_failure_skips_its_model_call_but_still_cleans() -> None:
    attachments = (
        make_attachment(0),
        make_attachment(
            1,
            media_type="audio/mpeg",
            media_family="audio",
            extension="mp3",
            data=b"ID3\x04\x00\x00\x00\x00\x00\x00fixture",
        ),
        make_attachment(
            2,
            media_type="image/png",
            media_family="image",
            extension="png",
            data=b"\x89PNG\r\n\x1a\nfixture",
        ),
    )
    storage = FakeScratchStorage(fail_upload_types=frozenset({"audio/mpeg"}))
    model = FakeGeminiModel()

    with pytest.raises(
        AttachmentAnalysisError,
        match="^attachment_upload_failed$",
    ):
        run_analysis(AttachmentAnalyzer(storage, model), attachments)

    assert len(storage.uploads) == 3
    assert len(model.calls) == 2
    assert {call.media_type for call in model.calls} == {
        "application/pdf",
        "image/png",
    }
    assert len(storage.deletes) == 3
    assert storage.objects == {}


def test_att_03_cleanup_failure_is_a_warning_and_does_not_mask_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FakeScratchStorage(fail_delete=True)

    with caplog.at_level(logging.WARNING, logger="alza_ai.attachments"):
        insight = run_analysis(
            AttachmentAnalyzer(storage, FakeGeminiModel()),
            (make_attachment(),),
        )[0]

    assert insight.warnings == (CLEANUP_WARNING, "Provider warning")
    assert [record.getMessage() for record in caplog.records] == [CLEANUP_WARNING]
    assert PRIVATE_MARKER not in caplog.text
    assert storage.objects


def test_att_03_cleanup_failure_does_not_mask_the_primary_model_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FakeScratchStorage(fail_delete=True)
    model = FakeGeminiModel(fail_media_types=frozenset({"application/pdf"}))

    with (
        caplog.at_level(logging.WARNING, logger="alza_ai.attachments"),
        pytest.raises(
            AttachmentAnalysisError,
            match="^attachment_model_failed$",
        ),
    ):
        run_analysis(AttachmentAnalyzer(storage, model), (make_attachment(),))

    assert [record.getMessage() for record in caplog.records] == [CLEANUP_WARNING]
    assert PRIVATE_MARKER not in caplog.text


def test_att_03_timeout_is_sanitized_and_cleanup_finishes() -> None:
    storage = FakeScratchStorage()
    model = FakeGeminiModel(block=True)

    with pytest.raises(
        AttachmentAnalysisError,
        match="^attachment_analysis_timeout$",
    ):
        run_analysis(
            AttachmentAnalyzer(
                storage,
                model,
                analysis_timeout_seconds=0.01,
            ),
            (make_attachment(),),
        )

    assert len(model.calls) == 1
    assert len(storage.deletes) == 1
    assert storage.objects == {}
    assert model.active == 0


def test_att_03_cancellation_is_preserved_after_cleanup_finishes() -> None:
    async def exercise() -> None:
        storage = FakeScratchStorage()
        model = FakeGeminiModel(block=True)
        task = asyncio.create_task(
            AttachmentAnalyzer(storage, model).analyze((make_attachment(),))
        )
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(storage.deletes) == 1
        assert storage.objects == {}
        assert model.active == 0

    asyncio.run(exercise())


def test_att_03_cleanup_failure_does_not_mask_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        storage = FakeScratchStorage(fail_delete=True)
        model = FakeGeminiModel(block=True)
        task = asyncio.create_task(
            AttachmentAnalyzer(storage, model).analyze((make_attachment(),))
        )
        await model.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(storage.deletes) == 1

    with caplog.at_level(logging.WARNING, logger="alza_ai.attachments"):
        asyncio.run(exercise())

    assert [record.getMessage() for record in caplog.records] == [CLEANUP_WARNING]
    assert PRIVATE_MARKER not in caplog.text


def test_att_03_cleanup_timeout_is_observable_without_masking_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FakeScratchStorage(block_delete=True)

    with caplog.at_level(logging.WARNING, logger="alza_ai.attachments"):
        insight = run_analysis(
            AttachmentAnalyzer(
                storage,
                FakeGeminiModel(),
                cleanup_timeout_seconds=0.01,
            ),
            (make_attachment(),),
        )[0]

    assert insight.warnings[0] == CLEANUP_WARNING
    assert [record.getMessage() for record in caplog.records] == [CLEANUP_WARNING]
    assert PRIVATE_MARKER not in caplog.text


def test_att_02_storage_region_must_be_europe_west3() -> None:
    with pytest.raises(ValueError, match="^attachment_storage_region_invalid$"):
        AttachmentAnalyzer(
            FakeScratchStorage(region="us-central1"),
            FakeGeminiModel(),
        )


def test_att_02_empty_input_performs_no_adapter_work() -> None:
    storage = FakeScratchStorage()
    model = FakeGeminiModel()

    assert run_analysis(AttachmentAnalyzer(storage, model), ()) == ()
    assert storage.uploads == []
    assert storage.deletes == []
    assert model.calls == []


class MockBlob:
    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str, float]] = []
        self.delete_timeouts: list[float] = []

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        timeout: float,
    ) -> None:
        self.uploads.append((data, content_type, timeout))

    def delete(self, *, timeout: float) -> None:
        self.delete_timeouts.append(timeout)


class MockBucket:
    def __init__(self) -> None:
        self.blob_names: list[str] = []
        self.blobs: dict[str, MockBlob] = {}

    def blob(self, object_name: str) -> MockBlob:
        self.blob_names.append(object_name)
        return self.blobs.setdefault(object_name, MockBlob())


class MockStorageClient:
    def __init__(self) -> None:
        self.bucket_names: list[str] = []
        self.mock_bucket = MockBucket()

    def bucket(self, bucket_name: str) -> MockBucket:
        self.bucket_names.append(bucket_name)
        return self.mock_bucket


def test_port_01_cloud_storage_adapter_uploads_and_deletes_with_explicit_timeout() -> (
    None
):
    client = MockStorageClient()
    storage = CloudStorageScratchStorage(
        bucket_name=SCRATCH_BUCKET,
        client=cast(object, client),
        request_timeout_seconds=7.0,
    )
    object_name = "a" * 32
    data = b"adapter-private-bytes"

    async def exercise() -> str:
        uri = await storage.stage(
            object_name=object_name,
            data=data,
            media_type="application/pdf",
        )
        await storage.delete(object_name=object_name)
        return uri

    assert asyncio.run(exercise()) == f"gs://{SCRATCH_BUCKET}/{object_name}"
    assert storage.region == REGION
    assert client.bucket_names == [SCRATCH_BUCKET]
    assert client.mock_bucket.blob_names == [object_name, object_name]
    blob = client.mock_bucket.blobs[object_name]
    assert blob.uploads == [(data, "application/pdf", 7.0)]
    assert blob.delete_timeouts == [7.0]


class MockGeminiResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class MockGeminiModels:
    def __init__(
        self,
        response_text: str | None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.response_text = response_text
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    async def generate_content(self, **kwargs: object) -> MockGeminiResponse:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return MockGeminiResponse(self.response_text)


class MockGeminiClient:
    def __init__(self, models: MockGeminiModels) -> None:
        self.aio = type("MockAio", (), {"models": models})()


class MockGeminiClientFactory:
    def __init__(self, client: MockGeminiClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> MockGeminiClient:
        self.calls.append(kwargs)
        return self.client


def test_port_01_gemini_adapter_uses_global_v1_gcs_structured_request() -> None:
    response = {
        "summary": "Adapter summary",
        "extracted_text": "Adapter extraction",
        "relevant_facts": ["Adapter fact"],
        "warnings": [],
    }
    models = MockGeminiModels(json.dumps(response))
    factory = MockGeminiClientFactory(MockGeminiClient(models))
    model = GeminiMultimodalModel(
        project_id="example-project",
        client_factory=cast(object, factory),
    )
    uri = f"gs://{SCRATCH_BUCKET}/{'b' * 32}"

    result = asyncio.run(
        model.analyze(
            gcs_uri=uri,
            media_type="application/pdf",
        )
    )

    assert result == GeminiAttachmentResult(
        summary="Adapter summary",
        extracted_text="Adapter extraction",
        relevant_facts=("Adapter fact",),
        warnings=(),
    )
    assert len(factory.calls) == 1
    client_call = factory.calls[0]
    assert client_call["vertexai"] is True
    assert client_call["project"] == "example-project"
    assert client_call["location"] == "global"
    http_options = cast(types.HttpOptions, client_call["http_options"])
    assert http_options.api_version == "v1"
    assert http_options.timeout == 30_000

    assert len(models.calls) == 1
    request = models.calls[0]
    assert request["model"] == "gemini-3.6-flash"
    contents = cast(list[str | types.Part], request["contents"])
    assert len(contents) == 2
    assert isinstance(contents[0], str)
    assert PRIVATE_MARKER not in contents[0]
    assert isinstance(contents[1], types.Part)
    file_data = contents[1].file_data
    assert file_data is not None
    assert file_data.file_uri == uri
    assert file_data.mime_type == "application/pdf"
    config = cast(types.GenerateContentConfig, request["config"])
    assert config.response_mime_type == "application/json"
    schema = cast(dict[str, object], config.response_json_schema)
    assert schema["required"] == [
        "summary",
        "extracted_text",
        "relevant_facts",
        "warnings",
    ]


@pytest.mark.parametrize(
    "response_text",
    [
        None,
        "not-json",
        "{}",
        json.dumps(
            {
                "summary": 1,
                "extracted_text": "text",
                "relevant_facts": [],
                "warnings": [],
            }
        ),
    ],
)
def test_port_01_gemini_adapter_rejects_malformed_output_without_leakage(
    response_text: str | None,
) -> None:
    models = MockGeminiModels(response_text)
    factory = MockGeminiClientFactory(MockGeminiClient(models))
    model = GeminiMultimodalModel(
        project_id="example-project",
        client_factory=cast(object, factory),
    )

    with pytest.raises(
        GeminiModelError,
        match="^attachment_model_invalid_response$",
    ) as raised:
        asyncio.run(
            model.analyze(
                gcs_uri=f"gs://{SCRATCH_BUCKET}/{'c' * 32}",
                media_type="application/pdf",
            )
        )

    assert raised.value.code == "attachment_model_invalid_response"
    if response_text is not None:
        assert response_text not in str(raised.value)


def test_port_01_gemini_adapter_sanitizes_provider_failure() -> None:
    models = MockGeminiModels(
        None,
        failure=RuntimeError(f"provider failed: {PRIVATE_MARKER}"),
    )
    factory = MockGeminiClientFactory(MockGeminiClient(models))
    model = GeminiMultimodalModel(
        project_id="example-project",
        client_factory=cast(object, factory),
    )

    with pytest.raises(GeminiModelError, match="^attachment_model_failed$") as raised:
        asyncio.run(
            model.analyze(
                gcs_uri=f"gs://{SCRATCH_BUCKET}/{'d' * 32}",
                media_type="application/pdf",
            )
        )

    assert PRIVATE_MARKER not in str(raised.value)
