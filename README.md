# MaiBot 猜角色

玩家心里选一个角色，麦麦逐轮提问、查资料并尝试猜出。独立 MaiBot SDK 插件，面向 Linux 部署；不依赖 DeepSeek Provider 或 Codex 桥接插件，不修改 MaiBot 主程序。

## 已实现的玩法

- 不限作品开局，每群单独一局，只有开局者的有效回答推进游戏。
- 五种回答：**是、否、不知道、可能是、可能不是**。不知道不排除候选，可能类回答作为软线索交给模型。
- 默认每段 20 问；最后一道回答后仍可尝试猜测，需要继续提问时由玩家选择继续或揭晓。
- 修改任意已回答的问题、撤回上一个答案、一次可拒绝的额外提示、资料冲突确认。
- 猜中必须经玩家确认；猜错记录到排除名单。模型自报置信度不直接判定胜利。
- 问答、改答记录、角色排除、检索证据、来源、搜索预算和 Codex 额度冷却持久化。重启后发送 `猜角色 状态`，需要恢复推理时发送 `猜角色 重试`。
- 当前聊天流保留最近一局；新开局会替换该聊天流的旧局，不提供长期对局排行榜。

## 搜索与推理

游戏推理由 `game.model_name` 指定的 **MaiBot 已配置模型**处理；留空时使用 `game.task_name`（默认 `utils`）。它选择下一问、检索需求、提示、冲突确认或候选角色。

搜索顺序固定为：

1. 目标 Linux 机器的 **Codex CLI / gpt-5.6-luna / live web search**，强制使用 ChatGPT 登录方式，不使用 OpenAI API Key。
2. 明确识别为额度耗尽时保存冷却状态；优先使用错误中的机器可读重置时间，否则默认 60 分钟后允许再次尝试。
3. 超时、暂时失败、无有效检索结果时，**本次**改用 DeepSeek Anthropic 服务端搜索，不给 Codex 设置额度耗尽标记。
4. 两边失败时向玩家说明，保留进度；模型仍可以提问。资料不足的猜测会标明未充分核实。

正式猜测前，在本轮与本局搜索预算允许时追加一次候选核实，并让游戏模型重新审阅结果。源码参考已有插件的协议用法，自行实现客户端；没有运行或安装参考插件。

来源来自搜索后端，不保证网页本身正确。DeepSeek 摘要 URL 必须匹配服务端工具实际返回的来源；Codex 必须包含已完成的 `web_search` 事件、正常结束事件和符合 schema 的结果。不会把“模型说自己搜过”当作实际搜索。Codex 输出中的来源仍需真实对局验证，不能据此承诺角色猜中率。

## Linux 部署

要求：MaiBot 1.2.5、SDK >=2.8.1,<3、Python >=3.12。Codex CLI 建议 **0.153.4 或兼容本文所用参数的更新版**。目标账号需要能使用 Luna。

在 Linux 的 MaiBot 根目录克隆插件：

```bash
git clone https://github.com/sky2002/maibot-character-guess.git plugins/maibot-character-guess
```

如果已经用 zip 安装过，请先备份并移走旧插件目录，再克隆并恢复自己的 `config.toml`。不要在已有非 Git 目录中直接运行上述命令。也可以继续使用发布 zip 安装。

在 MaiBot 根目录，用运行 MaiBot 的 Python 环境安装插件运行依赖，例如：

```bash
uv pip install --python .venv/bin/python -r plugins/maibot-character-guess/requirements.txt
cp plugins/maibot-character-guess/config.example.toml plugins/maibot-character-guess/config.toml
```

若 MaiBot 使用的不是 `.venv/bin/python`，替换为实际 Runner 使用的解释器。启动后在插件管理中启用该插件。

以后更新时先停用插件或停止 MaiBot，在 MaiBot 根目录运行：

```bash
git -C plugins/maibot-character-guess pull --ff-only
uv pip install --python .venv/bin/python -r plugins/maibot-character-guess/requirements.txt
```

参考更新后的 `config.example.toml` 补充配置，然后重新启用插件或重启 MaiBot。`config.toml` 不纳入版本控制，更新不会覆盖它；请勿再次用 `cp` 覆盖自己的配置。

**Codex 必须与 MaiBot 服务运行在同一个 Linux 用户下，或明确设置 `codex.home` 指向该用户有权访问的独立 Codex 目录。** 登录桌面电脑不会自动登录 Linux 服务用户。

先在该 Linux 用户下执行：

```bash
codex --version
codex login status
# 尚未登录时再运行：
codex login
```

使用 ChatGPT 登录。不要把令牌复制进插件配置。`codex.executable` 可填写 `command -v codex` 查到的绝对路径；systemd 服务的 PATH 常与交互式终端不同。

设置 DeepSeek 密钥环境变量 `DEEPSEEK_API_KEY`，确保它存在于 **启动 MaiBot 的服务环境**。例如用已有 systemd 单元的 `EnvironmentFile` 提供权限受限的环境文件。配置里只填写环境变量名称，不保存密钥。没有该变量时 Codex 路径仍可运行，但备用搜索会明确报告未配置。

插件对 Codex 使用独立临时工作目录、只读沙箱，关闭 shell/unified_exec、apps、多代理与技能发现，忽略用户配置，不提供任意命令执行入口。检索请求只包含游戏问题、答案、提示和排除角色，不包含群号、用户号或正常聊天历史。建议为机器人使用专门的 Linux 服务用户及干净的 Codex 登录目录；部署机的管理员配置、CLI 版本和本地扩展仍由部署者管理。

## 部署检查

在插件目录，用与 MaiBot 相同的 Python 环境运行（以下命令中的 `python` 必须指向该解释器）：

```bash
python -m character_guess.check --config config.toml
```

默认只验证配置、CLI 路径和环境变量是否存在，**不消耗模型额度**。真实检查需要显式 `--probe`，建议停用游戏请求时进行；使用 MaiBot 分配的实际持久化目录，不要通过更换目录重置预算：

```bash
python -m character_guess.check --config config.toml --probe codex \
  --data-dir ../../data/plugins/sky2002.character-guess
python -m character_guess.check --config config.toml --probe deepseek \
  --data-dir ../../data/plugins/sky2002.character-guess
```

探测会消耗对应服务额度，并输出检索结果与来源。本地开发阶段使用模拟服务和真实 SDK 测试，没有借用当前电脑的登录发起真实检索，也没有连接你的远程 Linux 机器。

## 指令

| 指令 | 用途 |
| --- | --- |
| `猜角色 开始` | 选好秘密角色后开始 |
| `是` / `否` / `不知道` / `可能是` / `可能不是` | 当前开局者回答当前问题 |
| `猜角色 答 3 可能不是` | 带题号回答，推荐在网络延迟较大时使用 |
| `猜角色 修改 2 否` | 修改答案并重新推理；清除依赖旧答案的检索证据 |
| `猜角色 撤回` | 撤回上次答案，回到对应问题 |
| `猜角色 提示 作品或其他线索` | 在模型请求额外提示时提供线索 |
| `猜角色 跳过提示` | 拒绝额外提示 |
| `猜角色 保持` | 资料冲突时确认原答案 |
| `猜对了` / `猜错了` | 确认机器人报出的角色 |
| `猜角色 状态` / `记录` / `来源` | 查看本局当前状态、问答或检索 URL（后两项也需带“猜角色”前缀） |
| `猜角色 继续` | 到问题上限后增加 20 问 |
| `猜角色 重试` | 推理失败或服务重启后恢复 |
| `猜角色 揭晓 角色名` / `猜角色 结束` | 结束本局 |

只有活动局开局者的精确短答会被拦截，其他群友和普通聊天不推进游戏。后台正在处理时不会排队接受多条回答；同一消息 ID 去重，早于当前问题时间的回答丢弃。关闭 `accept_bare_answers` 后使用带前缀指令；猜测确认可用 `猜角色 猜对了`。

## 预算与延迟

默认每局最多 **6 次检索请求**，每轮最多 **2 次**，跨群最多同时检索 **2 次**，相同线索结果缓存 24 小时。一次 Codex/DeepSeek 请求内部可能进行多次网页检索，不能把插件检索请求数等同于服务端实际搜索次数。

Codex 与 DeepSeek 各自默认 180 秒超时，允许设置 5–600 秒；当先超时再切备用时，一次检索可接近 **360 秒**，还不含模型推理和最多 5 秒的排队。180 秒是单后端超时，不是整个游戏轮次的承诺；部署时可按实测调整。

从 0.1.0 更新后，请重启 MaiBot 或重新加载插件，使新配置校验规则生效，然后刷新 WebUI。已有 `config.toml` 中保存的超时值会保留；如需使用新默认值，请将 `codex.timeout_seconds` 和 `deepseek.timeout_seconds` 都改为 `180`。设置为 `300` 也可通过校验。

DeepSeek 每个 UTC 自然日跨群最多 **30 次 HTTP 请求**，每次最多输出 4096 Token；请求前预留相应输出预算，默认每日预留上限 122880。失败、超时或截断不返还预留量。**这不是人民币费用硬上限**：输入 Token、搜索内部总结和实际计费由服务商决定。若需要金额硬限制，请同时在服务商账户设置额度。`max_uses` 会提交给服务端，其具体支持由 DeepSeek 决定。暂停（`pause_turn`）、截断或缺少有效来源的响应明确报失败，不自动发起额外收费的续接请求。

搜索失败原因只显示安全摘要，不把密钥、原始鉴权错误或 Codex 进程输出发到群里。配额冷却与备用预算保存在 SQLite，重启不重置。游戏模型的普通推理费用按 MaiBot 原有配置计算，不计入备用搜索预算。

## 开发验证

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

测试包含状态转换、多人隔离、题号与消息去重、改答、提示、冲突、最后一题猜测、重启、SDK 生命周期、备用预算、错误分类、服务端搜索校验和真实子进程的超时清理。测试不需要模型密钥或 Codex 登录。

配置模板与配置模型保持一致；中、英、日三份提示词语义对齐，用户界面默认为简体中文。

源码仓库：[sky2002/maibot-character-guess](https://github.com/sky2002/maibot-character-guess)。这是独立插件仓库，尚未提交到 MaiBot 插件市场。

## 参考

- [MaiBot SDK](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md)
- [DeepSeek Anthropic Provider 的协议实现](https://github.com/LowValueTarget777/deepseek-anthropic-provider)
- [DeepSeek Anthropic 文档](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api/)
- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex 网页搜索](https://learn.chatgpt.com/docs/web-search)
- [Luna 模型说明](https://developers.openai.com/api/docs/models/gpt-5.6-luna)

许可证：GPL-3.0-or-later。
