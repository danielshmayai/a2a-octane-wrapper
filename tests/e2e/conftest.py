"""
DeepEval configuration for E2E tests.

Configures Gemini 2.0 Flash as the evaluation model via a custom
DeepEvalBaseLLM wrapper.  No Confident AI login required — all
results are stored locally in ./deepeval_results/.

How it works:
  1. Loads GEMINI_API_KEY from the repo-root .env file.
  2. Disables Confident AI cloud integration and telemetry so tests
     run fully offline (no account, no dashboard upload).
  3. Exposes a session-scoped ``gemini_judge`` pytest fixture that the
     E2E tests inject into DeepEval metrics (ToolCorrectnessMetric,
     AnswerRelevancyMetric) as the evaluation LLM.

The wrapper subclasses DeepEvalBaseLLM, implementing the four required
methods: get_model_name(), load_model(), generate(), a_generate().
When DeepEval passes an optional ``schema`` kwarg (for structured JSON
output), the wrapper forwards it to Gemini's response_schema config.
"""

import os
import sys
import pathlib

import pytest
from dotenv import load_dotenv

# Ensure repo root is on sys.path so tests can import project modules
# (a2a_models, main, tool_router, etc.) without installing the package.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env from repo root so GEMINI_API_KEY is available without
# the user having to export it manually in every shell session.
load_dotenv(_REPO_ROOT / ".env")

# ── Disable Confident AI cloud & telemetry ────────────────────────────
# DEEPEVAL_RESULTS_FOLDER  → save results as local JSON instead of uploading.
# DEEPEVAL_TELEMETRY_OPT_OUT → prevent anonymous usage telemetry.
os.environ.setdefault("DEEPEVAL_RESULTS_FOLDER", "./deepeval_results")
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

from google import genai
from google.genai import types as genai_types
from deepeval.models.base_model import DeepEvalBaseLLM


# Gemini 2.0 Flash — free-tier, fast, sufficient for evaluation tasks.
_GEMINI_MODEL = "gemini-2.0-flash"


class GeminiFlashModel(DeepEvalBaseLLM):
    """Custom DeepEval model wrapper that delegates to Google Gemini 2.0 Flash.

    DeepEval's built-in models default to OpenAI GPT.  This wrapper lets us
    use Gemini instead, keeping API costs at zero (free-tier Flash) and
    avoiding any OpenAI dependency.

    The wrapper implements four methods required by DeepEvalBaseLLM:
      - get_model_name()  → human-readable identifier for logs/reports.
      - load_model()      → returns the underlying client object.
      - generate()        → synchronous text generation (used by most metrics).
      - a_generate()      → async variant (DeepEval calls this in async contexts).

    When DeepEval passes ``schema=<pydantic model>`` via kwargs, the wrapper
    enables Gemini's structured-output mode (response_mime_type=application/json)
    so the evaluation framework can parse the judge's verdict reliably.
    """

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY must be set to run E2E tests with Gemini judge. "
                "Add it to your .env file or export it in your shell."
            )
        self._client = genai.Client(api_key=api_key)

    def get_model_name(self) -> str:
        return _GEMINI_MODEL

    def load_model(self):
        return self._client

    def generate(self, prompt: str, **kwargs) -> str:
        """Synchronous generation.  Called by DeepEval metrics to evaluate test cases.

        If DeepEval passes a ``schema`` kwarg (a Pydantic model class), we
        enable Gemini's structured-output mode so the response is valid JSON
        that DeepEval can parse into its internal verdict objects.
        """
        schema = kwargs.get("schema")
        config = genai_types.GenerateContentConfig()
        if schema:
            # Structured output — Gemini returns JSON matching the schema.
            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            )
        response = self._client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=prompt)],
                )
            ],
            config=config,
        )
        return self._extract(response)

    async def a_generate(self, prompt: str, **kwargs) -> str:
        """Async generation.  The google-genai SDK is synchronous, so we
        delegate to a thread to avoid blocking the event loop."""
        import asyncio

        return await asyncio.to_thread(self.generate, prompt, **kwargs)

    @staticmethod
    def _extract(response) -> str:
        """Pull plain-text parts from a Gemini GenerateContentResponse."""
        texts = []
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if hasattr(part, "text") and part.text:
                    texts.append(part.text)
        return "\n".join(texts) if texts else ""


# ── Shared fixture ────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def gemini_judge():
    """Session-scoped Gemini 2.0 Flash judge shared across all E2E tests.

    Scope is ``session`` so the client is created once and reused,
    avoiding repeated initialization overhead across test classes.
    Tests that need LLM-based evaluation (TestToolCorrectnessDeepEval,
    TestAnswerRelevancy) declare this fixture as a parameter.
    """
    return GeminiFlashModel()
