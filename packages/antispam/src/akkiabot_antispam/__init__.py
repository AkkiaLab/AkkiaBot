"""LLM-based spam classifier (pure library, no core / framework dependency)."""

from importlib.metadata import version

from akkiabot_antispam.classifier import AntiSpamClassifier, ClassifierSettings, build_client
from akkiabot_antispam.models import SpamContext, SpamVerdict, Usage, VerdictSource
from akkiabot_antispam.prompts import DEFAULT_PROMPT_VERSION, Prompt
from akkiabot_antispam.tracing import LangfuseTracer, NullTracer, Tracer

__version__ = version("akkiabot-antispam")

__all__ = [
    "DEFAULT_PROMPT_VERSION",
    "AntiSpamClassifier",
    "ClassifierSettings",
    "LangfuseTracer",
    "NullTracer",
    "Prompt",
    "SpamContext",
    "SpamVerdict",
    "Tracer",
    "Usage",
    "VerdictSource",
    "__version__",
    "build_client",
]
