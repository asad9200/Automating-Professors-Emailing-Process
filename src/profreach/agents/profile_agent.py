import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from profreach.config import Settings, get_settings
from profreach.db.models import UserProfileData
from profreach.llm import LLMConfigurationError, create_chat_model, structured

logger = logging.getLogger(__name__)


class ProfileExtractionError(RuntimeError):
    """Raised when a CV cannot be converted into a user profile."""


class ProfileState(TypedDict, total=False):
    cv_text: str
    profile: UserProfileData


class ProfileAgent:
    def __init__(self, settings: Settings | None = None, structured_model: Any = None):
        self.settings = settings or get_settings()
        self.structured_model = structured_model
        graph = StateGraph(ProfileState)
        graph.add_node("extract_profile", self._extract_profile)
        graph.add_edge(START, "extract_profile")
        graph.add_edge("extract_profile", END)
        self.graph = graph.compile()

    def parse(self, cv_text: str) -> UserProfileData:
        if not cv_text.strip():
            raise ProfileExtractionError("The CV contains no extractable text.")
        logger.info("Starting CV profile extraction")
        try:
            result = self.graph.invoke({"cv_text": cv_text})
        except ProfileExtractionError:
            raise
        except Exception as error:
            logger.exception("CV profile extraction failed")
            raise ProfileExtractionError("Profile extraction failed. Please try again.") from error
        logger.info("Completed CV profile extraction")
        return result["profile"]

    def _extract_profile(self, state: ProfileState) -> dict[str, UserProfileData]:
        model = self.structured_model or self._create_model()
        structured_model = structured(model, UserProfileData)
        result = structured_model.invoke(
            [
                (
                    "system",
                    "Extract only facts explicitly stated in the CV. Do not infer or invent "
                    "values. Use null for missing scalar fields and empty lists for missing "
                    "collections. Preserve concrete details in the structured fields.",
                ),
                ("human", f"Extract a user profile from this CV:\n\n{state['cv_text']}"),
            ]
        )
        return {"profile": UserProfileData.model_validate(result)}

    def _create_model(self):
        try:
            return create_chat_model(self.settings)
        except LLMConfigurationError as error:
            raise ProfileExtractionError(
                f"{error} You can still enter your profile manually."
            ) from error
