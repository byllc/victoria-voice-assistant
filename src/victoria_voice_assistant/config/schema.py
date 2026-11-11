from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(Schema):
    type: Literal["openai", "anthropic"]
    base_url: str | None = None
    api_key: str | None = None

    @model_validator(mode="after")
    def require_openai_url(self) -> ServerConfig:
        if self.type == "openai" and self.base_url is None:
            raise ValueError("base_url is required for openai servers")
        return self


class PriceConfig(Schema):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class ModelConfig(Schema):
    server: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=30, gt=0)
    pin: bool = False
    # LM Studio loads one copy of each model for every entry that names it (and for Hermes), with the
    # largest context any of them asks for (Bill, 2026-11-02: "if the same model is specified three
    # times in config it should be the same model").
    context_length: int | None = Field(default=None, gt=0)
    price: PriceConfig | None = None


class PoolMemberConfig(Schema):
    model: str
    good_for: str


class BudgetConfig(Schema):
    monthly_usd: float = Field(gt=0)
    downgrade_at: float = Field(ge=0, le=1)
    stop_at: float = Field(ge=0, le=1)


class PoolConfig(Schema):
    members: list[PoolMemberConfig] = Field(min_length=1)
    default: str
    min_confidence: float = Field(default=0.6, ge=0, le=1)
    spoken_overrides: dict[str, str] = Field(default_factory=dict)
    escalate_on_failure: bool = True
    budget: BudgetConfig | None = None

    @model_validator(mode="after")
    def ordered_budget_thresholds(self) -> PoolConfig:
        if (
            self.budget is not None
            and self.budget.stop_at < self.budget.downgrade_at
        ):
            raise ValueError("budget.stop_at must be >= budget.downgrade_at")
        return self


class DeciderConfig(Schema):
    type: Literal["llm_choice", "decision_model"]
    model: str | None = None
    server: str | None = None

    @model_validator(mode="after")
    def require_engine_reference(self) -> DeciderConfig:
        if self.type == "llm_choice" and self.model is None:
            raise ValueError("model is required for llm_choice")
        if self.type == "decision_model" and (self.server is None or self.model is None):
            # model names the models.yaml entry whose `model` is the weights folder mlx-lm serves
            raise ValueError("server and model are required for decision_model")
        return self


class RolesConfig(Schema):
    router: str
    router_fallback: str
    summarizer: str | None = None
    pre_router: str = "off"

    @field_validator("pre_router", mode="before")
    @classmethod
    def normalize_yaml_off(cls, value: Any) -> Any:
        return "off" if value is False else value


class SttConfig(Schema):
    model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "en"


class TtsConfig(Schema):
    voice: str = "af_heart"
    speed: float = Field(default=1.0, gt=0.25, le=3)


class VadConfig(Schema):
    confidence: float = Field(default=0.7, ge=0, le=1)
    start_s: float = Field(default=0.2, ge=0)
    stop_s: float = Field(default=0.2, ge=0)


class TurnConfig(Schema):
    smart_turn: bool = True
    # Silence after which the turn ends even if Smart Turn says the speaker isn't done.
    fallback_s: float = Field(default=1.8, gt=0)
    # Speech shorter than this ("stop", "pause") ends at the first pause without asking
    # Smart Turn, which judges very short clips poorly. 0 turns this off.
    short_clip_s: float = Field(default=0.5, ge=0)


class SpeechConfig(Schema):
    """In-process speech for voice sessions (Whisper MLX, Kokoro, Silero VAD, Smart Turn)."""

    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)


class ModelsConfig(Schema):
    servers: dict[str, ServerConfig] = Field(min_length=1)
    models: dict[str, ModelConfig] = Field(min_length=1)
    pools: dict[str, PoolConfig] = Field(default_factory=dict)
    decider: DeciderConfig
    roles: RolesConfig
    speech: SpeechConfig = Field(default_factory=SpeechConfig)


class ReplyConfig(Schema):
    model: bool = False
    template: bool | str = False
    silent: bool = False

    @model_validator(mode="after")
    def select_one_reply_mode(self) -> ReplyConfig:
        enabled = sum((self.model, bool(self.template), self.silent))
        if enabled > 1:
            raise ValueError("choose only one of model, template, or silent")
        return self


class EntityScope(Schema):
    label: str | None = None
    domains: list[str] = Field(default_factory=list)


class HttpToolConfig(Schema):
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None


class ToolConfig(Schema):
    name: str
    description: str
    kind: Literal[
        "mcp",
        "ha_service",
        "ha_script",
        "http",
        "python",
        "builtin",
        "stub",
    ]
    service: str | None = None
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    confirm: bool = False
    enabled: bool = True
    timeout_s: float | None = Field(default=None, gt=0)
    # How long this tool's answers stay in the conversation, in seconds, for answers that go out of date
    # (the time, the weather): after it the whole exchange is taken out, so the model has to call the
    # tool again and can't repeat its old answer. 0: gone by the next request. Unset: kept.
    forget_after_s: float | None = Field(default=None, ge=0)
    reply: ReplyConfig | None = None
    examples: list[str] = Field(default_factory=list)
    entities: EntityScope | None = None
    notice: str | None = None
    setup: str | None = None
    then: dict[str, str | bool] | None = None
    mcp_server: str | None = None
    http: HttpToolConfig | None = None
    # Set by the loader when the entry failed validation; the registry turns it into an automatic stub.
    broken: str | None = None


class SearchReadConfig(Schema):
    fetcher: Literal["auto", "http", "playwright"] = "auto"
    max_chars: int = Field(default=4000, gt=0)
    browser_pool: int = Field(default=2, ge=0)
    block: list[str] = Field(default_factory=list)


class SearchConfig(Schema):
    provider: str = "searxng"
    url: HttpUrl
    engines: list[str] = Field(default_factory=list)
    max_results: int = Field(default=3, gt=0)
    snippet_chars: int = Field(default=300, gt=0)
    cache_ttl_s: int = Field(default=600, ge=0)
    # When SearXNG is down or all its engines fail, search this site in the warm headless browser.
    browser_fallback: Literal["bing"] | None = None
    read: SearchReadConfig | None = None


class WeatherHomeConfig(Schema):
    name: str = "Home"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class WeatherConfig(Schema):
    provider: str = "open-meteo"
    home: WeatherHomeConfig | None = None
    units: Literal["imperial", "metric"] = "imperial"
    cache_ttl_s: int = Field(default=600, ge=0)


class HistoryConfig(Schema):
    """Command history: every request is kept in the database; the `history` tool looks back `search_days`."""

    search_days: int = Field(default=7, ge=1)


class TimersConfig(Schema):
    max_per_device: int = Field(default=10, gt=0)
    ring_max_s: int = Field(default=300, gt=0)
    sound: str = "chime.flac"


NotifyStyle = Literal["ask_first", "speak_summary", "chime_only"]
CLOCK_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHoursConfig(Schema):
    start: str = Field(default="00:00", pattern=CLOCK_TIME)
    end: str = Field(default="08:00", pattern=CLOCK_TIME)


class NotificationsConfig(Schema):
    """Architecture §10.3. Quiet hours default to midnight to 8am (Bill, 2026-10-31)."""

    quiet_hours: QuietHoursConfig | None = Field(default_factory=QuietHoursConfig)
    default_style: NotifyStyle = "ask_first"
    mention_pending_after_reply: bool = True
    question_timeout_min: float = Field(default=30, gt=0)
    expire_after_h: float = Field(default=24, gt=0)
    web_push: bool = False
    # Web Push (VAPID): the private key is a secret (`assistant push keys` creates it; docs/secrets.md); push services
    # require a contact for the sender, a mailto: or https: URL.
    vapid_private_key: str | None = None
    vapid_subject: str | None = Field(default=None, pattern=r"^(mailto:|https://)")


class MusicRoomConfig(Schema):
    """Older form of a room with a player (`music.rooms`); folded into the device registry (`rooms`), which
    then fills this in for the music service: every room with a player, and the satellites in it."""

    satellite: str | None = None  # the device in this room (its requests play here)
    player: str  # Music Assistant player id or name
    aliases: list[str] = Field(default_factory=list)  # other ways to say the room
    satellites: list[str] = Field(default_factory=list)  # filled in from `rooms` and `satellites`


class RoomConfig(Schema):
    """One room in the device registry (architecture §4): what it has. Satellites name their room."""

    aliases: list[str] = Field(default_factory=list)  # other ways to say the room
    player: str | None = None  # its Music Assistant player (id or name)
    area: str | None = None  # its Home Assistant area: "turn off the lights" there means these
    echo: str | None = None  # its Echo (a media_player entity), for relays such as "ask Alexa"


class MusicConfig(Schema):
    server: HttpUrl | None = None
    token: str | None = None
    source_order: list[str] = Field(default_factory=list)
    rooms: dict[str, MusicRoomConfig] = Field(default_factory=dict)
    duck_level: int = Field(default=20, ge=0, le=100)
    # Music Assistant's Sendspin listener, for the web app as a speaker. Default: the server's host, port 8927
    # (its /sendspin on the API port wants Music Assistant's own login first).
    sendspin_url: str | None = None

    def sendspin(self) -> str | None:
        if self.sendspin_url:
            return self.sendspin_url
        if self.server is None:
            return None
        return f"ws://{self.server.host}:8927/sendspin"


class McpServerConfig(Schema):
    url: HttpUrl
    token: str | None = None


class SatelliteConfig(Schema):
    """An ESPHome voice satellite (Stage 12): the assistant connects to it with its API key."""

    host: str
    port: int = Field(default=6053, gt=0, lt=65536)
    key: str | None = None  # the device's API encryption key, e.g. "${SATELLITE1_API_KEY}"
    # Its microphone level is raised by this factor before speech detection: the Satellite1's clean
    # (unclipped) channel is quiet (Stage 12 findings). At 8, speech sat at Pipecat's speech-detection
    # loudness gate (`min_volume` 0.6, about -50 LUFS) and some turns heard no speech (2026-11-02). At 24
    # it answered at 4 feet but not at 8 or 20, though Whisper could read all three recordings; at 120
    # all pass the gate (2026-11-11). Loud speech is eased in under full scale, not clipped.
    gain: float = Field(default=120.0, gt=0, le=512)
    # The LED ring's colour while notifications are unread for this satellite (architecture §10.3's
    # pending indicator); null leaves the ring alone.
    pending_color: str | None = Field(default="#FFA000", pattern=r"^#[0-9A-Fa-f]{6}$")
    room: str | None = None  # its room in `rooms`; default: the room with the satellite's own name
    # When several satellites hear the same words, added to this one's score (how loud it hears the
    # speech, dB): for a microphone or room that reads loud or quiet, so the nearest satellite answers (architecture §4, wake-word arbitration).
    arbitration_offset_db: float = 0.0
    # For fault-finding only: a folder that gets each turn's microphone audio (after `gain`) as a WAV file,
    # named by satellite, time and how the turn ended. Off unless set; it keeps recordings of what was said.
    record_dir: str | None = None


class ToolTestGiven(Schema):
    timers: list[str] = Field(default_factory=list)


class ToolTestConfig(Schema):
    say: str
    expect: str
    given: ToolTestGiven | None = None

    @field_validator("expect")
    @classmethod
    def known_expectation(cls, value: str) -> str:
        if value in ("clarify", "offer_agent", "answer"):
            return value
        if value.startswith(("tool:", "agent:")) and value.split(":", 1)[1]:
            return value
        raise ValueError("expect must be tool:<name>, agent:<name>, clarify, offer_agent, or answer")


def _broken_tool_entry(raw: Any, error: ValidationError) -> Any:
    """Keep a tool whose definition is invalid as a stub, so one bad entry doesn't reject the file."""
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return raw  # nothing to name the stub after: let validation fail normally
    problems = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '$'}: {item['msg']}" for item in error.errors()
    )
    description = raw.get("description")
    params = raw.get("params")
    valid_params = isinstance(params, dict) and all(isinstance(value, dict) for value in params.values())
    return {
        "name": raw["name"],
        "description": description if isinstance(description, str) and description else raw["name"],
        "kind": "stub",
        "params": params if valid_params else {},
        "broken": f"invalid tool definition: {problems}",
    }


class ToolsConfig(Schema):
    tools: list[ToolConfig] = Field(default_factory=list)
    search: SearchConfig | None = None
    weather: WeatherConfig | None = None
    timers: TimersConfig | None = None
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    music: MusicConfig | None = None
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    rooms: dict[str, RoomConfig] = Field(default_factory=dict)  # the device registry: room -> what it has
    satellites: dict[str, SatelliteConfig] = Field(default_factory=dict)  # device name -> satellite
    satellite_audio_port: int = Field(default=8767, gt=0, lt=65536)  # where every satellite fetches replies (LAN, HTTP)
    satellite_arbitration_s: float = Field(default=0.5, ge=0, le=3)  # how long to wait for other satellites waking on the same words
    tests: list[ToolTestConfig] = Field(default_factory=list)

    @field_validator("tools", mode="before")
    @classmethod
    def stub_broken_tools(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        entries = []
        for raw in value:
            try:
                ToolConfig.model_validate(raw)
            except ValidationError as exc:
                raw = _broken_tool_entry(raw, exc)
            entries.append(raw)
        return entries

    @model_validator(mode="after")
    def unique_tool_names(self) -> ToolsConfig:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return self

    @model_validator(mode="after")
    def build_device_registry(self) -> ToolsConfig:
        """One registry (Stage 13): `music.rooms` (the older form) folds into `rooms`, each satellite gets
        its room, and `music.rooms` is then filled from `rooms` for the music service."""
        music_rooms = self.music.rooms if self.music else {}
        for name, old in music_rooms.items():
            room = self.rooms.setdefault(name, RoomConfig())
            room.player = room.player or old.player
            room.aliases = list(dict.fromkeys([*room.aliases, *old.aliases]))
            if old.satellite in self.satellites and self.satellites[old.satellite].room is None:
                self.satellites[old.satellite].room = name
        for name, satellite in self.satellites.items():
            satellite.room = satellite.room or name
        if self.music is not None:
            self.music.rooms = {
                name: MusicRoomConfig(
                    player=room.player,
                    aliases=room.aliases,
                    satellites=[s for s, sat in self.satellites.items() if sat.room == name],
                )
                for name, room in self.rooms.items()
                if room.player
            }
        return self


class AgentConfig(Schema):
    name: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["model", "harness", "a2a", "relay"]
    model: str | None = None
    pool: str | None = None
    mode: Literal["wait", "background", "auto"] = "wait"
    wait_s: float | None = Field(default=None, gt=0)
    system: str | None = None
    tools: list[str] = Field(default_factory=list)
    search: Literal["builtin", "native"] | None = None
    share_context: str | None = None
    url: HttpUrl | None = None
    via: str | None = None
    service: str | None = None
    content_type: str | None = None
    targets: dict[str, str] = Field(default_factory=dict)
    reply: str | None = None
    notify: NotifyStyle | None = None  # how a background result is announced; default_style if unset
    # A LaunchAgent to restart when one of this agent's tasks is cancelled or times out: Hermes keeps
    # retrying a cancelled job on its own (up to 45 minutes), stalling the next one (Bill, Stage 11).
    restart_on_cancel: str | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> AgentConfig:
        if self.kind == "model" and (self.model is None) == (self.pool is None):
            raise ValueError("model agents require exactly one of model or pool")
        if self.kind == "harness" and self.model is None:
            raise ValueError("harness agents require model")
        if self.kind == "a2a" and self.url is None:
            raise ValueError("a2a agents require url")
        if self.mode == "auto" and self.wait_s is None:
            raise ValueError("auto mode requires wait_s")
        return self


class AgentsConfig(Schema):
    default: str | None = None
    agents: list[AgentConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_agent_names_and_aliases(self) -> AgentsConfig:
        names = [agent.name.casefold() for agent in self.agents]
        aliases = [
            alias.casefold()
            for agent in self.agents
            for alias in [agent.name, *agent.aliases]
        ]
        if len(names) != len(set(names)):
            raise ValueError("agent names must be unique")
        if len(aliases) != len(set(aliases)):
            raise ValueError("agent names and aliases must be unique")
        if self.default is not None and self.default not in names:
            raise ValueError(f"default agent {self.default!r} is not defined")
        return self


class AppConfig(Schema):
    models: ModelsConfig
    tools: ToolsConfig
    agents: AgentsConfig


class ReferenceValidationError(ValueError):
    def __init__(self, filename: str, field: str, message: str) -> None:
        self.filename = filename
        self.field = field
        self.message = message
        super().__init__(message)


def validate_references(
    models: ModelsConfig,
    tools: ToolsConfig,
    agents: AgentsConfig,
) -> None:
    def fail(filename: str, field: str, message: str) -> None:
        raise ReferenceValidationError(filename, field, message)

    model_names = set(models.models)
    pool_names = set(models.pools)
    server_names = set(models.servers)
    tool_names = {tool.name for tool in tools.tools}

    for model_name, model in models.models.items():
        if model.server not in server_names:
            fail(
                "models.yaml",
                f"models.{model_name}.server",
                f"references unknown server {model.server!r}",
            )

    for pool_name, pool in models.pools.items():
        member_names = {member.model for member in pool.members}
        if pool.default not in model_names:
            fail(
                "models.yaml",
                f"pools.{pool_name}.default",
                f"references unknown model {pool.default!r}",
            )
        if pool.default not in member_names:
            fail(
                "models.yaml",
                f"pools.{pool_name}.default",
                "must name a member of this pool",
            )
        for index, member in enumerate(pool.members):
            if member.model not in model_names:
                fail(
                    "models.yaml",
                    f"pools.{pool_name}.members.{index}.model",
                    f"references unknown model {member.model!r}",
                )
        for alias, model_name in pool.spoken_overrides.items():
            if model_name not in model_names:
                fail(
                    "models.yaml",
                    f"pools.{pool_name}.spoken_overrides.{alias}",
                    f"references unknown model {model_name!r}",
                )
            if model_name not in member_names:
                fail(
                    "models.yaml",
                    f"pools.{pool_name}.spoken_overrides.{alias}",
                    "must name a member of this pool",
                )

    if models.decider.model is not None and models.decider.model not in model_names:
        fail(
            "models.yaml",
            "decider.model",
            f"references unknown model {models.decider.model!r}",
        )
    if models.decider.server is not None and models.decider.server not in server_names:
        fail(
            "models.yaml",
            "decider.server",
            f"references unknown server {models.decider.server!r}",
        )
    for role in ("router", "router_fallback", "summarizer", "pre_router"):
        reference = getattr(models.roles, role)
        if reference is not None and reference != "off":
            if reference not in model_names and reference not in pool_names:
                fail(
                    "models.yaml",
                    f"roles.{role}",
                    f"references unknown model or pool {reference!r}",
                )

    for index, tool in enumerate(tools.tools):
        if tool.kind == "mcp" and tool.mcp_server is not None:
            if tool.mcp_server not in tools.mcp_servers:
                fail(
                    "tools.yaml",
                    f"tools.{index}.mcp_server",
                    f"references unknown server {tool.mcp_server!r}",
                )

    agent_names = {agent.name for agent in agents.agents}
    history_tools = {tool.name for tool in tools.tools if tool.kind == "builtin" and tool.service == "history"}
    for index, agent in enumerate(agents.agents):
        if agent.model is not None and agent.model not in model_names:
            fail(
                "agents.yaml",
                f"agents.{index}.model",
                f"references unknown model {agent.model!r}",
            )
        if agent.pool is not None and agent.pool not in pool_names:
            fail(
                "agents.yaml",
                f"agents.{index}.pool",
                f"references unknown pool {agent.pool!r}",
            )
        for tool_name in agent.tools:
            if tool_name not in tool_names:
                fail(
                    "agents.yaml",
                    f"agents.{index}.tools",
                    f"references unknown tool {tool_name!r}",
                )
            elif tool_name in history_tools:
                fail(
                    "agents.yaml",
                    f"agents.{index}.tools",
                    f"{tool_name!r} reads the household's command history, which never goes to an agent",
                )
    if agents.default is not None and agents.default not in agent_names:
        fail(
            "agents.yaml",
            "default",
            f"references unknown agent {agents.default!r}",
        )
