from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用猜角色插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class GameSection(PluginConfigBase):
    question_limit: int = Field(default=20, ge=3, le=100, description="每段问题上限；到达后由玩家决定是否继续")
    idle_minutes: int = Field(default=30, ge=1, le=1440, description="无操作多少分钟后允许新开局")
    accept_bare_answers: bool = Field(default=True, description="游戏中接收开局者的五种简短回答")
    model_name: str = Field(default="", description="游戏模型名；留空使用下方模型任务")
    task_name: str = Field(default="utils", description="MaiBot 已配置的模型任务名")
    model_timeout_seconds: int = Field(default=45, ge=5, le=180, description="单次游戏推理超时")
    prompt_locale: Literal["zh-CN", "en-US", "ja-JP"] = Field(default="zh-CN", description="推理提示词语言")


class SearchSection(PluginConfigBase):
    max_requests_per_game: int = Field(default=6, ge=0, le=50, description="每局检索请求上限，含正式猜测前核实")
    max_requests_per_turn: int = Field(default=2, ge=1, le=5, description="每轮检索请求上限")
    cache_hours: int = Field(default=24, ge=0, le=168, description="相同线索检索缓存小时数，0关闭")
    max_parallel: int = Field(default=2, ge=1, le=8, description="所有群同时进行检索的上限")


class CodexSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="优先使用 Codex CLI 搜索")
    executable: str = Field(default="codex", description="Linux Codex CLI 可执行文件或绝对路径")
    home: str = Field(default="", description="独立检索用户的 CODEX_HOME，留空继承；不要填密钥")
    model: str = Field(default="gpt-5.6-luna", description="Codex 搜索模型")
    timeout_seconds: int = Field(default=30, ge=5, le=180, description="一次 Codex 检索总超时")
    quota_retry_minutes: int = Field(default=60, ge=1, le=1440, description="未提供重置时间时，额度耗尽后的探测间隔")


class DeepSeekSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用 DeepSeek 备用搜索")
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", description="存放 DeepSeek 密钥的环境变量名")
    base_url: str = Field(default="https://api.deepseek.com/anthropic", description="Anthropic 兼容接口基础地址")
    model: str = Field(default="deepseek-flash", description="支持服务端搜索的 DeepSeek 模型")
    tool_type: str = Field(default="web_search_20260209", description="服务端搜索工具版本")
    max_uses: int = Field(default=2, ge=1, le=5, description="每次请求的服务端搜索次数参数，支持情况取决于服务端")
    timeout_seconds: int = Field(default=30, ge=5, le=180, description="一次备用检索总超时")
    max_output_tokens: int = Field(default=4096, ge=512, le=16384, description="每次备用响应输出 Token 上限")
    daily_requests: int = Field(default=30, ge=0, le=10000, description="UTC 每日备用 HTTP 请求硬上限，0禁用请求")
    daily_output_tokens: int = Field(default=122880, ge=0, description="UTC 每日输出 Token 预留预算，0禁用请求")


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    game: GameSection = Field(default_factory=GameSection)
    search: SearchSection = Field(default_factory=SearchSection)
    codex: CodexSection = Field(default_factory=CodexSection)
    deepseek: DeepSeekSection = Field(default_factory=DeepSeekSection)
