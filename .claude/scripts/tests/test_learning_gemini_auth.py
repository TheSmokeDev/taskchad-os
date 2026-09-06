"""Explicit Workspace OAuth context keeps the model-only tool boundary intact."""
import json

from runtime.base import RuntimeRequest
from runtime.gemini_cli import _ModelOnlyLaunchEnv


def test_model_only_honors_explicit_host_project_and_keeps_empty_tools(tmp_path):
    request = RuntimeRequest("Reply OK", tmp_path, "learning-auth", model_only=True,
                             allowed_tools=[], disallowed_tools=["*"],
                             env={"GOOGLE_CLOUD_PROJECT": "explicit-workspace-project"})
    with _ModelOnlyLaunchEnv(request, {"GOOGLE_CLOUD_PROJECT": "unrelated-project",
                                      "GOOGLE_GENAI_USE_VERTEXAI": "true"}) as env:
        from pathlib import Path
        settings = json.loads(Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
        assert env["GOOGLE_CLOUD_PROJECT"] == "explicit-workspace-project"
        assert "GOOGLE_GENAI_USE_VERTEXAI" not in env
        assert settings["tools"]["core"] == []
        assert settings["mcp"]["allowed"] == []


def test_model_only_does_not_inherit_either_project_spelling(tmp_path):
    request = RuntimeRequest("Reply OK", tmp_path, "learning-auth", model_only=True)
    with _ModelOnlyLaunchEnv(request, {"GOOGLE_CLOUD_PROJECT": "unrelated-project",
                                      "GOOGLE_CLOUD_PROJECT_ID": "also-unrelated"}) as env:
        assert "GOOGLE_CLOUD_PROJECT" not in env
        assert "GOOGLE_CLOUD_PROJECT_ID" not in env
