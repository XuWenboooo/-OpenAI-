"""IR v0 —— 协议转换课题的核心中间表示（M0 契约层）

设计要点（来自《协议转换课题_执行方案》）：
  * 三个协议（OpenAI Chat / OpenAI Response / Anthropic Messages）统一映射到 IR，
    每个协议只写「to_ir / from_ir」两个方向，6 个方向压缩为 3 对 adapter。
  * IR 分三层：
      L0  content block 模型 —— 承载三种协议的消息内容载体
      L1  规范请求/响应 —— 统一的请求、工具、用量、响应结构
      L2  会话与缓存上下文 —— 会话归属、previous_response_id、缓存断点布局约定
  * 缓存断点布局是 L2 的核心契约：
      Anthropic 前缀缓存「断点之前任意字节变了，之后全部失效」，
      渲染顺序必须固定为 tools → system → messages，易变内容放最后。

本模块只定义数据结构与常量，不发起任何网络调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    """ISO-8601 UTC 时间戳，用于会话与埋点记录。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# L0 · 内容块模型（Content Block）
# ---------------------------------------------------------------------------

#: 受支持的内容块类型。多模态（image/document/audio）明确不做（见方案 3.6）。
ContentBlockType = Literal["text", "tool_use", "tool_result", "thinking"]


@dataclass
class ContentBlock:
    """L0 内容块。

    对齐三种协议：
      * Anthropic 的 content 是 block 数组（text / tool_use / tool_result / thinking）；
      * OpenAI Chat 的 content 是字符串，工具调用为顶层 tool_calls —— 转换时拆成块；
      * OpenAI Response 的 content 是数组（output_text / function_call 等）。
    """

    type: ContentBlockType
    text: Optional[str] = None                     # text / thinking
    id: Optional[str] = None                       # tool_use / tool_result 的块 id
    name: Optional[str] = None                     # 工具名
    input: Optional[dict[str, Any]] = None         # 工具入参
    tool_use_id: Optional[str] = None              # tool_result 指向的 tool_use.id
    is_error: bool = False                         # tool_result 是否错误
    thinking: Optional[str] = None                 # thinking 内容
    extra: dict[str, Any] = field(default_factory=dict)  # 各协议私有字段透传

    # ---- 便捷构造 ----
    @classmethod
    def text_block(cls, text: str) -> "ContentBlock":
        return cls(type="text", text=text)

    @classmethod
    def tool_use_block(cls, id: str, name: str, input: dict[str, Any]) -> "ContentBlock":
        return cls(type="tool_use", id=id, name=name, input=input)

    @classmethod
    def tool_result_block(
        cls, tool_use_id: str, content: str, is_error: bool = False
    ) -> "ContentBlock":
        return cls(type="tool_result", tool_use_id=tool_use_id, text=content, is_error=is_error)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            d["text"] = self.text
        if self.id is not None:
            d["id"] = self.id
        if self.name is not None:
            d["name"] = self.name
        if self.input is not None:
            d["input"] = self.input
        if self.tool_use_id is not None:
            d["tool_use_id"] = self.tool_use_id
        if self.is_error:
            d["is_error"] = True
        if self.thinking is not None:
            d["thinking"] = self.thinking
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContentBlock":
        known = {k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__}
        return cls(**known, extra=d)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<ContentBlock {self.type} {self.text or self.name or self.id or ''}>"


# ---------------------------------------------------------------------------
# L1 · 规范请求 / 响应 / 工具 / 用量
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant"]


@dataclass
class IRMessage:
    """一条 IR 消息。content 统一为 ContentBlock 列表（长度为 1 时多为纯文本）。"""

    role: Role
    content: list[ContentBlock] = field(default_factory=list)
    name: Optional[str] = None  # 可选：会话归属名，透传用

    @classmethod
    def text(cls, role: Role, text: str, name: Optional[str] = None) -> "IRMessage":
        return cls(role=role, content=[ContentBlock.text_block(text)], name=name)

    def plain_text(self) -> str:
        """提取本消息的纯文本（工具调用块返回空）。"""
        return "".join(b.text or "" for b in self.content if b.type in ("text", "thinking"))

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": [b.to_dict() for b in self.content]}


@dataclass
class IRTool:
    """工具定义（规范化）。OpenAI function / Anthropic tool 统一到这一结构。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema
    id: Optional[str] = None  # 各协议的工具 id（若有）
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        d["input_schema"] = self.input_schema
        if self.id:
            d["id"] = self.id
        d.update(self.extra)
        return d


@dataclass
class IRUsage:
    """token 用量（对齐两家字段，缓存字段是北极星指标的数据来源）。

    Anthropic: input_tokens / output_tokens / cache_creation_input_tokens /
               cache_read_input_tokens
    OpenAI:    prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        """命中判定：cache_read > 0 即视为命中缓存。"""
        return self.cache_read_input_tokens > 0

    @property
    def cache_created(self) -> bool:
        """本请求写入了新缓存前缀（cache_creation > 0）。"""
        return self.cache_creation_input_tokens > 0

    @property
    def cache_miss(self) -> bool:
        """未命中：creation 与 read 同时为 0（方案 3.3 的判定口径）。"""
        return self.cache_creation_input_tokens == 0 and self.cache_read_input_tokens == 0

    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


@dataclass
class IRRequest:
    """L1 · 规范请求（协议无关的唯一请求结构）。"""

    model: str
    system: list[ContentBlock] = field(default_factory=list)   # 缓存前缀（tools/system 之后）
    messages: list[IRMessage] = field(default_factory=list)
    tools: list[IRTool] = field(default_factory=list)
    stream: bool = False
    max_tokens: int = 1024
    temperature: float = 1.0
    # Anthropic 缓存断点：控制注入位置（默认按 tools → system → messages 顺序）
    cache_control: list[int] = field(default_factory=list)  # 需要插断点的消息下标
    extra: dict[str, Any] = field(default_factory=dict)     # 协议私有透传

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "system": [b.to_dict() for b in self.system],
            "messages": [m.to_dict() for m in self.messages],
            "tools": [t.to_dict() for t in self.tools],
            "stream": self.stream,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "cache_control": self.cache_control,
        }


@dataclass
class IRResponse:
    """L1 · 规范响应。"""

    id: str
    model: str
    role: Role = "assistant"
    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: Optional[str] = None
    usage: IRUsage = field(default_factory=IRUsage)
    # OpenAI Response 状态：上一轮响应 id（用于下一轮 previous_response_id）
    previous_response_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)  # 原始响应（调试/透传）

    def plain_text(self) -> str:
        return "".join(b.text or "" for b in self.content if b.type in ("text", "thinking"))


# ---------------------------------------------------------------------------
# L2 · 会话与缓存上下文
# ---------------------------------------------------------------------------

#: 缓存断点布局常量（方案 1.3 / 图 2）：渲染顺序固定 tools → system → messages。
CACHE_ORDER_TOOLS = "tools"
CACHE_ORDER_SYSTEM = "system"
CACHE_ORDER_MESSAGES = "messages"
CACHE_RENDER_ORDER = [CACHE_ORDER_TOOLS, CACHE_ORDER_SYSTEM, CACHE_ORDER_MESSAGES]

#: Anthropic 缓存回看窗口：单轮新增 content block 超过该值，尾部断点失效且不报错（官方硬约束）。
CACHE_LOOKBACK_WINDOW_BLOCKS = 20

#: Anthropic 每请求 cache_control 断点上限：第 5 个直接返回 400（v3 修正，官方硬约束）。
#: 断点策略 = 3 个固定分层断点（tools 后 / system 后 / 历史静态段后）+ 1 个滚动尾部断点。
MAX_CACHE_BREAKPOINTS = 4

#: 缓存最小可缓存阈值（token，需逐模型查表，默认保守值 512）。
DEFAULT_MIN_CACHE_THRESHOLD = 512

#: 可参考的模型阈值表（来自方案洞察 4：非单调，不能外推，逐模型核对官方文档）。
MODEL_CACHE_THRESHOLDS: dict[str, int] = {
    "claude-opus-4.5": 4096,
    "claude-opus-4.6": 4096,
    "claude-opus-4.7": 2048,
    "claude-opus-4.8": 1024,
    "claude-opus-5": 512,
}


@dataclass
class IRSessionContext:
    """L2 · 会话与缓存上下文。

    对应 Session Init 的身份绑定三级资产（team / agent / task），
    以及协议转换必须支持的 previous_response_id 状态。
    """

    session_id: str
    team_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    # OpenAI Response 有状态字段：上一轮响应 id（省 token 设计）
    previous_response_id: Optional[str] = None
    # 记忆注入策略占位：内容、粒度、更新频率（等待 TRACK 01 输入）
    memory_injection: Optional[dict[str, Any]] = None
    # 缓存断点布局：按固定顺序渲染（tools → system → messages），易变内容放最后
    render_order: list[str] = field(
        default_factory=lambda: list(CACHE_RENDER_ORDER)
    )
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    expires_at: Optional[str] = None  # TTL 淘汰（状态层用）

    def touch(self) -> None:
        self.updated_at = utcnow_iso()


# ---------------------------------------------------------------------------
# 便捷函数：从 JSON 重建
# ---------------------------------------------------------------------------

def block_from_json(d: dict[str, Any]) -> ContentBlock:
    return ContentBlock.from_dict(dict(d))


def message_from_json(d: dict[str, Any]) -> IRMessage:
    return IRMessage(
        role=d["role"],
        content=[ContentBlock.from_dict(dict(b)) for b in d.get("content", [])],
        name=d.get("name"),
    )
