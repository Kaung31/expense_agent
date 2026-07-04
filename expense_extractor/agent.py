"""The Extractor as a Microsoft Agent Framework agent (Azure Foundry vision).

`agent.py` is the *only* place that imports agent-framework / Azure. It adapts a
Foundry-backed vision model to the `VisionModel` interface in `extractor.py`, so all
the extraction logic and its tests stay provider-agnostic and offline.

The agent has **no tools** and takes **no actions** — it only transcribes. That is
the injection-safe boundary the guide insists on (§5, §8).
"""

from __future__ import annotations

from pathlib import Path

from expense_extractor.config import ModelBackend, Settings, get_settings
from expense_extractor.extractor import (
    SYSTEM_PROMPT,
    ImageInput,
    MockVisionModel,
    RawExtraction,
    VisionModel,
    extract,
    load_image,
)
from expense_extractor.schemas import ExtractionResult


class FoundryVisionModel:
    """`VisionModel` backed by Azure AI Foundry via the Agent Framework `Agent`."""

    def __init__(
        self,
        model_deployment: str,
        project_endpoint: str,
        *,
        credential=None,
        temperature: float = 0.0,
        name: str | None = None,
    ) -> None:
        # Imported lazily so the core package doesn't require agent-framework/azure.
        from agent_framework import Agent, Content, Message
        from agent_framework.foundry import FoundryChatClient

        if credential is None:
            from azure.identity import DefaultAzureCredential  # az login locally, MI in prod

            credential = DefaultAzureCredential()

        self.name = name or model_deployment
        self._Message = Message
        self._Content = Content
        self._temperature = temperature

        client = FoundryChatClient(
            project_endpoint=project_endpoint, model=model_deployment, credential=credential
        )
        # instructions == the injection-safe system prompt; tools=None on purpose.
        self._agent = Agent(client=client, name="extractor", instructions=SYSTEM_PROMPT)

    async def run(self, system: str, user: str, image: ImageInput) -> str:
        Message, Content = self._Message, self._Content
        message = Message(
            "user",
            contents=[
                Content.from_text(user),
                Content.from_uri(image.data_uri, media_type=image.media_type),
            ],
        )
        response = await self._agent.run(
            [message],
            options={"response_format": RawExtraction, "temperature": self._temperature},
        )
        return response.text or str(response)


def build_extractor_models(
    settings: Settings | None = None,
) -> tuple[VisionModel, VisionModel | None]:
    """Return (primary, escalation) vision models per the configured backend."""
    settings = settings or get_settings()

    if settings.expense_model_backend is ModelBackend.MOCK:
        return (
            MockVisionModel(name=settings.foundry_model),
            MockVisionModel(name=settings.foundry_model_escalation),
        )

    endpoint = settings.foundry_project_endpoint or settings.azure_openai_endpoint
    if not endpoint:
        raise RuntimeError(
            "No Foundry/Azure endpoint configured. Set FOUNDRY_PROJECT_ENDPOINT "
            "(or switch EXPENSE_MODEL_BACKEND=mock)."
        )
    primary = FoundryVisionModel(settings.foundry_model, endpoint)
    escalation = FoundryVisionModel(settings.foundry_model_escalation, endpoint)
    return primary, escalation


async def extract_document(
    document: str | Path | ImageInput,
    *,
    submitter: str | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """One-call convenience: load a document and extract it with escalation.

    Works offline (mock) or against Azure depending on EXPENSE_MODEL_BACKEND.
    """
    settings = settings or get_settings()
    image = document if isinstance(document, ImageInput) else load_image(document)
    primary, escalation = build_extractor_models(settings)
    return await extract(image, primary, escalation_model=escalation, submitter=submitter)
