from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "MedContour 医学影像标注平台"
    database_url: str = "sqlite:////tmp/annotation.db"
    data_root: Path = Path("/data")
    pancreas_prediction_root: Path = Path("/pancreas-predictions")
    task_file_root: Path = Path("/task-files")
    preview_root: Path = Path("/previews")
    manifests_root: Path = Path("/manifests")
    annotation_root: Path = Path("/annotations")
    initial_admin_email: str = "admin@example.com"
    initial_admin_password: str = ""
    session_hours: int = 12
    cookie_secure: bool = False
    max_annotation_bytes: int = 256 * 1024 * 1024
    max_task_attachment_bytes: int = 100 * 1024 * 1024
    auto_import_dataset: bool = False
    codex_worker_url: str = "http://codex-worker:8090"
    codex_worker_timeout_seconds: int = 600
    codex_workspace_root: Path = Path("/workspace")
    codex_task_file_root: Path = Path("/workspace/.medcontour-task-files")
    trusted_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:18080", "http://localhost:18080"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
