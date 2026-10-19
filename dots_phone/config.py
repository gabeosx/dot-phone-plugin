from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Public fixture key; evaluation must never receive real records or credentials.
EVALUATION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def live_number(value):
    """US/Canada E.164, excluding NANPA's fictional 555-0100..0199 block."""
    return bool(re.fullmatch(r"\+1[2-9][0-9]{9}", value)) and value[5:10] != "55501"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = "production"
    database: Path = Path("data/phone.sqlite3")
    public_base_url: str = ""
    mcp_resource_url: str = ""
    mcp_metadata_base_url: str = ""
    owner: str = "owner"
    encryption_key: SecretStr = SecretStr("")
    owner_secret_hash: SecretStr = SecretStr("")
    dry_run: bool = True
    voice_backend: str = "tac"
    pipecat_engine: str = "live"
    pipecat_realtime_model: str = "gpt-realtime-2.1"
    pipecat_turn_detection: Literal["semantic_vad", "server_vad"] = "semantic_vad"
    pipecat_vad_eagerness: Literal["low", "medium", "high", "auto"] = "high"
    pipecat_vad_silence_ms: int = Field(default=1000, ge=200, le=3000)
    pipecat_general_vad_threshold: float = Field(default=0.5, ge=0, le=1)
    pipecat_general_vad_idle_timeout_ms: int = Field(default=0, ge=0, le=30000)
    pipecat_general_turn_detection: Literal["semantic_vad", "server_vad"] = "server_vad"
    pipecat_noise_reduction: Literal["near_field", "far_field", "off"] = "off"
    pipecat_input_filter: Literal["off", "deepfilter"] = "off"
    deepfilter_model_path: Path | None = None
    deepfilter_library_path: Path = Path("/opt/deepfilter/lib/libdf.so")
    deepfilter_frame_timeout_ms: int = Field(default=100, ge=20, le=1000)
    pipecat_external_turns: bool = False
    test_stop_on_human: bool = False
    test_response_limit: int = Field(default=0, ge=0, le=100)
    pipecat_owned_dtmf: bool = False
    account_tasks: bool = False
    local_reasoner_enabled: bool = False
    local_reasoner_model: str = "gpt-6-sol"
    local_reasoner_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    local_reasoner_response_policy: Literal["direct_with_codex", "always_consult"] = (
        "direct_with_codex"
    )
    local_reasoner_voice_effort: Literal["minimal", "low", "medium", "high"] = "medium"
    local_reasoner_directory: Path | None = None
    post_call_audio_model_directory: Path | None = None
    post_call_audio_confirmation_model_directory: Path | None = None
    call_authorization_mode: Literal["owner_page", "connected_owner"] = "owner_page"
    connected_call_budget_usd: Decimal = Field(default=Decimal("3"), gt=0, le=20)
    connected_daily_budget_usd: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    destination_policy: Literal["task_scope", "allowlist"] = "task_scope"
    pipecat_account_dtmf: bool = False
    trace_realtime_stages: bool = False
    local_gmail_worker_directory: Path | None = None
    local_mail_worker_directory: Path | None = None
    local_context_worker_directory: Path | None = None
    media_base_url: str = ""
    allowed_numbers: list[str] = Field(default_factory=list)
    callback_hosts: list[str] = Field(default_factory=list)
    redirect_uris: list[str] = Field(default_factory=list)
    max_seconds: int = Field(default=600, ge=1, le=3600)
    decision_seconds: int = Field(default=30, ge=1, le=60)
    call_state_wait_seconds: int = Field(default=0, ge=0, le=10)
    max_concurrent: int = Field(default=1, ge=1, le=1)
    retain_transcript: bool = True
    retain_recording: bool = True
    retention_seconds: int = Field(default=86400, ge=60, le=604800)
    phone_number: str = ""
    openai_key: SecretStr = SecretStr("")
    twilio_account: SecretStr = SecretStr("")
    twilio_auth: SecretStr = SecretStr("")
    twilio_key: SecretStr = SecretStr("")
    twilio_secret: SecretStr = SecretStr("")

    @field_validator("pipecat_general_vad_idle_timeout_ms")
    @classmethod
    def validate_general_vad_idle_timeout(cls, value):
        if value != 0 and value < 5000:
            raise ValueError("idle timeout must be 0 (disabled) or 5000–30000 milliseconds")
        return value

    @model_validator(mode="after")
    def validate_profile(self):
        if self.mcp_resource_url:
            resource = urlsplit(self.mcp_resource_url)
            if (
                resource.scheme != "https"
                or not resource.hostname
                or resource.query
                or resource.fragment
                or resource.username
                or resource.password
            ):
                raise ValueError("MCP resource requires a canonical HTTPS identifier")
        if self.mcp_metadata_base_url:
            metadata = urlsplit(self.mcp_metadata_base_url)
            if (
                metadata.scheme not in {"http", "https"}
                or not metadata.hostname
                or metadata.path not in {"", "/"}
                or metadata.query
                or metadata.fragment
                or metadata.username
                or metadata.password
            ):
                raise ValueError("MCP metadata requires a configured origin")
            self.mcp_metadata_base_url = self.mcp_metadata_base_url.rstrip("/")
        if self.voice_backend not in {"tac", "pipecat"}:
            raise ValueError("unknown voice backend")
        if self.pipecat_engine not in {"live", "realtime"}:
            raise ValueError("unknown Pipecat engine")
        if self.test_stop_on_human and (
            self.voice_backend != "pipecat" or self.pipecat_engine != "realtime"
        ):
            raise ValueError("stop-on-human tests require Pipecat Realtime")
        if self.test_response_limit and not self.test_stop_on_human:
            raise ValueError("test response limit requires stop-on-human test mode")
        if self.pipecat_input_filter == "deepfilter" and (
            self.voice_backend != "pipecat" or self.deepfilter_model_path is None
        ):
            raise ValueError("DeepFilterNet requires Pipecat and DEEPFILTER_MODEL_PATH")
        self.validate_external_turns()
        if self.account_tasks and (
            self.voice_backend != "pipecat"
            or self.pipecat_engine != "realtime"
            or self.local_context_worker_directory
        ):
            raise ValueError("account tasks require shared Realtime without a fixture registry")
        if self.pipecat_account_dtmf and not self.account_tasks:
            raise ValueError("account keypad requires account tasks")
        if self.local_reasoner_enabled and not self.account_tasks:
            raise ValueError("local call reasoning requires shared account tasks")
        if self.local_reasoner_enabled and not self.local_reasoner_directory:
            raise ValueError("local call reasoning requires a private Codex worker directory")
        if self.local_reasoner_enabled and self.decision_seconds < 3:
            raise ValueError(
                "local call reasoning requires a decision timeout of at least 3 seconds"
            )
        if self.pipecat_realtime_model not in {"gpt-realtime-2.1", "gpt-realtime-2.1-mini"}:
            raise ValueError("unknown Pipecat Realtime model")
        if self.media_base_url:
            origin = urlsplit(self.media_base_url)
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.path not in {"", "/"}
                or origin.query
                or origin.fragment
                or origin.username
                or origin.password
            ):
                raise ValueError("media ingress requires an HTTPS origin")
            self.media_base_url = self.media_base_url.rstrip("/")
        if self.profile == "evaluation":
            if any(
                x.get_secret_value()
                for x in (
                    self.openai_key,
                    self.twilio_account,
                    self.twilio_auth,
                    self.twilio_key,
                    self.twilio_secret,
                )
            ):
                raise ValueError("evaluation profile refuses provider credentials")
            self.dry_run = True
            self.encryption_key = SecretStr(EVALUATION_KEY)
            self.public_base_url = self.public_base_url or "http://127.0.0.1:8000"
        elif self.profile != "production":
            raise ValueError("unknown profile")
        else:
            parsed = urlsplit(self.public_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("production requires verified HTTPS public origin")
            self.public_base_url = self.public_base_url.rstrip("/")
            if not self.owner_secret_hash.get_secret_value().startswith("$argon2id$"):
                raise ValueError("production requires Argon2id owner authentication")
            if not self.dry_run and (
                not live_number(self.phone_number)
                or (
                    self.destination_policy == "allowlist"
                    and (
                        not self.allowed_numbers
                        or not all(live_number(number) for number in self.allowed_numbers)
                    )
                )
                or not all(
                    x.get_secret_value()
                    for x in (
                        self.openai_key,
                        self.twilio_account,
                        self.twilio_auth,
                        self.twilio_key,
                        self.twilio_secret,
                    )
                )
            ):
                raise ValueError(
                    "live mode requires owned caller, provider credentials and valid destination policy"
                )
        if not self.encryption_key.get_secret_value():
            raise ValueError("encryption key required")
        return self

    @property
    def media_origin(self):
        return self.media_base_url or self.public_base_url

    def validate_external_turns(self):
        """The experimental external owner is limited to ordinary Realtime calls."""
        if self.pipecat_external_turns and (
            self.voice_backend != "pipecat"
            or self.pipecat_engine != "realtime"
            or self.account_tasks
            or self.pipecat_owned_dtmf
            or self.pipecat_account_dtmf
            or self.local_gmail_worker_directory
            or self.local_mail_worker_directory
            or self.local_context_worker_directory
        ):
            raise ValueError(
                "external turns require ordinary general Realtime without worker registries or keypad control"
            )

    def destination_allowed(self, number):
        if not re.fullmatch(r"\+1[2-9][0-9]{9}", number):
            return False
        if self.profile == "production" and not live_number(number):
            return False
        return self.destination_policy == "task_scope" or number in self.allowed_numbers

    @classmethod
    def from_env(cls):
        mapping = {
            "PROFILE": "profile",
            "DATABASE": "database",
            "PUBLIC_BASE_URL": "public_base_url",
            "MCP_RESOURCE_URL": "mcp_resource_url",
            "MCP_METADATA_BASE_URL": "mcp_metadata_base_url",
            "ALLOWED_USER_SUBJECT": "owner",
            "DATA_ENCRYPTION_KEY": "encryption_key",
            "OAUTH_OWNER_SECRET_HASH": "owner_secret_hash",
            "DRY_RUN": "dry_run",
            "VOICE_BACKEND": "voice_backend",
            "PIPECAT_ENGINE": "pipecat_engine",
            "PIPECAT_REALTIME_MODEL": "pipecat_realtime_model",
            "PIPECAT_TURN_DETECTION": "pipecat_turn_detection",
            "PIPECAT_VAD_EAGERNESS": "pipecat_vad_eagerness",
            "PIPECAT_VAD_SILENCE_MS": "pipecat_vad_silence_ms",
            "GENERAL_VAD_THRESHOLD": "pipecat_general_vad_threshold",
            "GENERAL_VAD_IDLE_TIMEOUT_MS": "pipecat_general_vad_idle_timeout_ms",
            "GENERAL_TURN_DETECTION": "pipecat_general_turn_detection",
            "PIPECAT_NOISE_REDUCTION": "pipecat_noise_reduction",
            "PIPECAT_INPUT_FILTER": "pipecat_input_filter",
            "DEEPFILTER_FRAME_TIMEOUT_MS": "deepfilter_frame_timeout_ms",
            "PIPECAT_EXTERNAL_TURNS": "pipecat_external_turns",
            "TEST_STOP_ON_HUMAN": "test_stop_on_human",
            "TEST_RESPONSE_LIMIT": "test_response_limit",
            "PIPECAT_OWNED_DTMF": "pipecat_owned_dtmf",
            "ACCOUNT_TASKS": "account_tasks",
            "LOCAL_REASONER_ENABLED": "local_reasoner_enabled",
            "LOCAL_REASONER_MODEL": "local_reasoner_model",
            "LOCAL_REASONER_EFFORT": "local_reasoner_effort",
            "LOCAL_REASONER_RESPONSE_POLICY": "local_reasoner_response_policy",
            "LOCAL_REASONER_VOICE_EFFORT": "local_reasoner_voice_effort",
            "LOCAL_REASONER_DIRECTORY": "local_reasoner_directory",
            "POST_CALL_AUDIO_MODEL_DIRECTORY": "post_call_audio_model_directory",
            "POST_CALL_AUDIO_CONFIRMATION_MODEL_DIRECTORY": "post_call_audio_confirmation_model_directory",
            "CALL_AUTHORIZATION_MODE": "call_authorization_mode",
            "CONNECTED_CALL_BUDGET_USD": "connected_call_budget_usd",
            "CONNECTED_DAILY_BUDGET_USD": "connected_daily_budget_usd",
            "DESTINATION_POLICY": "destination_policy",
            "PIPECAT_ACCOUNT_DTMF": "pipecat_account_dtmf",
            "TRACE_REALTIME_STAGES": "trace_realtime_stages",
            "LOCAL_GMAIL_WORKER_DIRECTORY": "local_gmail_worker_directory",
            "LOCAL_MAIL_WORKER_DIRECTORY": "local_mail_worker_directory",
            "LOCAL_CONTEXT_WORKER_DIRECTORY": "local_context_worker_directory",
            "MEDIA_BASE_URL": "media_base_url",
            "MAX_CALL_SECONDS": "max_seconds",
            "MAX_CONCURRENT_CALLS": "max_concurrent",
            "DECISION_TIMEOUT_SECONDS": "decision_seconds",
            "CALL_STATE_WAIT_SECONDS": "call_state_wait_seconds",
            "RETAIN_TRANSCRIPT": "retain_transcript",
            "RETAIN_RECORDING": "retain_recording",
            "RETENTION_SECONDS": "retention_seconds",
            "TWILIO_PHONE_NUMBER": "phone_number",
        }
        values = {
            target: os.environ["DOTS_PHONE_" + key]
            for key, target in mapping.items()
            if "DOTS_PHONE_" + key in os.environ
            and (
                key
                not in {
                    "LOCAL_GMAIL_WORKER_DIRECTORY",
                    "LOCAL_MAIL_WORKER_DIRECTORY",
                    "LOCAL_CONTEXT_WORKER_DIRECTORY",
                    "POST_CALL_AUDIO_MODEL_DIRECTORY",
                    "POST_CALL_AUDIO_CONFIRMATION_MODEL_DIRECTORY",
                }
                or os.environ["DOTS_PHONE_" + key].strip()
            )
        }
        for key, target in {
            "ALLOWED_TEST_NUMBERS": "allowed_numbers",
            "CALLBACK_HOSTS": "callback_hosts",
            "OAUTH_REDIRECT_URIS": "redirect_uris",
        }.items():
            values[target] = [
                x.strip() for x in os.getenv("DOTS_PHONE_" + key, "").split(",") if x.strip()
            ]
        for key, target in {
            "OPENAI_API_KEY": "openai_key",
            "TWILIO_ACCOUNT_SID": "twilio_account",
            "TWILIO_AUTH_TOKEN": "twilio_auth",
            "TWILIO_API_KEY": "twilio_key",
            "TWILIO_API_SECRET": "twilio_secret",
        }.items():
            values[target] = os.getenv(key, "")
        for key, target in {
            "DOTS_PHONE_DEEPFILTER_MODEL_PATH": "deepfilter_model_path",
            "DOTS_PHONE_DEEPFILTER_LIBRARY_PATH": "deepfilter_library_path",
        }.items():
            if os.getenv(key, "").strip():
                values[target] = os.environ[key]
        return cls(**values)
