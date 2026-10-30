# 系统提示词、用户 Skill 与 Built-in Skills

在已选择 workflow 的 UI 中输入 `/prompt` 或 `/skills`，用方向键和 Enter 操作。设置保存在 `workflow_root/<ID>/customization.json`，权限 0600，与记忆、coach 模板和 OMP 全局配置分别管理。关闭 UI 或重启 daemon 后仍然保留；同一个 workflow 下所有 task 共用，其他 workflow 不受影响。

## 模型与思考等级

`/select-model` 打开本机 OMP 模型目录，输入文字筛选、方向键和 Enter 选择；`/select-thinking-hardness` 按当前明确模型的元数据展示支持的等级。两者均标记当前 workflow 的选择。直接输入也支持 `/select-model provider/model` 和 `/select-thinking-hardness high`；参数 `default` 清除覆盖，继承本机 Runtime/OMP 默认。选择明确模型时同时把思考等级设为 auto，恢复默认模型时同时清除等级覆盖。

显式等级为 off/minimal/low/medium/high/xhigh/max/auto。未明确模型或目录中找不到它时，等级选择器展示通用等级；直接设置不要求模型目录可访问，最终由 OMP 校验或调整模型兼容性。目录来自所选 executable/profile 的 `omp models --json --no-extensions`，以当前 workflow workspace 为工作目录，只查询目录，不调用模型或启动训练回合；查询失败不写配置，不输出原始进程诊断。

模型与等级保存在可选 inference 字段，沿用 revision/CAS 和原子写入。设置优先级为 workflow 非空覆盖 → config.toml 的 agent.model/agent.thinking → OMP 默认；空串表示继承。本机配置文件允许省略 thinking，默认空串。所有 task 下次派发读取设置，已开始回合不变，恢复原 session 时也传入新参数。不会修改 OMP 全局配置、profile 或凭证。

该字段随用户 Git 配置同步；旧记录按原文读取，不补写新字段或改变其内容哈希。使用新字段的设备需升级至 0.16.0；provider 与凭证仍由各设备自行配置。CLI 示例：

```bash
oicoach-runtime workflow models coach --config /absolute/config.toml --json
oicoach-runtime workflow customize inference coach --model provider/model --thinking auto \
  --expected-revision 0 --config /absolute/config.toml --json
oicoach-runtime workflow customize inference coach --thinking high \
  --expected-revision 1 --config /absolute/config.toml --json
```

CLI 的 --model/--thinking 至少给一项；传空串清除该覆盖。选择过程不创建事件或重播旧请求。

## 系统提示词

`/prompt` 打开查看、编辑、恢复默认的选择器；也可直接用 `/prompt show`、`/prompt edit`、`/prompt reset`。

编辑时先选模式，再输入正文：

- `append`：默认方式，追加到 OMP 的原生系统提示词。
- `replace`：替换 OMP 原生系统正文；OMP 自己的工具信息、项目上下文等仍由原生机制构建。

Enter 到下一项或保存，Ctrl+J/Alt+Enter 换行，Shift+Tab 返回上一项，Esc 取消。正文最多 64 KiB，中文按 UTF-8 字节计算。原样保存 `$`、`${event_json}` 等文字，不进行模板替换、shell 执行或表达式求值。`append` 正文留空表示使用 OMP 原有系统提示；`replace` 不接受空正文。

`/prompt show` 显示用户自定义的部分，不冒充当前 OMP 原生系统提示词的完整展开。`/prompt reset` 确认后清空自定义部分，不删除 Skill，也不改动 OMP 原有项目/全局配置。

`event.md` 仍由外部 workflow 管理。旧 manifest 缺省 `instructions_in_system=false` 时，规则及 envelope 保持原来的用户消息通道；新建 coach 使用 true，将可信模板的静态前缀放入系统 prompt，仅将事件 JSON（mode、用户输入和 Runtime 数据）作为用户消息。模板必须恰有一个位于末尾的 `${event_json}`；在替换前拆分，payload 不会提升为系统指令。此功能把自定义文本通过 OMP 的 `--append-system-prompt` 或 `--system-prompt` 放进真正的系统提示词，不把普通 ask 正文或事件 payload 提升成系统指令。

0.18.0 起，每回合的原生系统提示都会附加命名约定：自称 GPT 或使用第一人称，不以 coaching title 自称。此约定在默认提示、append、replace 及 Skill/工具停用时都生效，保存在本轮 system.md 中；不会改写 event.md、用户保存的自定义正文、Skill 或旧历史。UI 的 `/prompt show` 仍只显示用户自定义部分。

系统 prompt 包含及时维护记忆及按需检索要求；启用记忆时追加当轮有界 `Runtime memory snapshot` 数据段。2026-10-30 起按用户要求取消固定 10k token 限制，使用 OMP 原生窗口及压缩；记忆快照仅受 IPC 传输字节边界约束。用户保存的自定义正文和 Skill 不变。

## 用户 Skill

`/skills` 先显示三个分类，再用方向键进入分类管理 Skill：

| 分类 | CLI 标识 | 用途 |
| --- | --- | --- |
| OI Introduce | `oi-introduce` | OI 背景说明、训练原则和约定 |
| user information | `user-information` | 用户明确提供的资料、目标与偏好 |
| personal skill | `personal-skill` | 个人方法、技巧和自定义规则 |

每类可以保存多项 Skill。旧记录没有 category 字段时视为 personal skill，读取时不改写原文件或 Git 历史。不会自动编造或填入个人资料。可直接输入：

```text
/skills add                       先用方向键选择分类
/skills add oi-introduce           直接在此分类新建
/skills user-information           查看用户资料分类
/skills personal-skill             查看个人方法分类
/skills category training-review   移动分类，保留正文与启停状态
/skills show training-review
/skills edit training-review
/skills disable training-review
/skills enable training-review
/skills delete training-review
```

新增时选择分类，再填写名称、用途描述、Markdown 正文；编辑保留名称、分类和原启停状态，可另用“移动分类”调整。名称最多 64 字符，仅小写字母/数字和分隔用短横线；用途描述最多 64 KiB（65536 字节，按 UTF-8 计算），正文最多 32 KiB，每个 workflow 最多 32 个，全部自定义设置合计最多 256 KiB。正文直接写指令内容，不需要手写 YAML frontmatter。删除需输入 `yes` 确认，停用保留内容以便再启用。

0.15.0 起，三类已启用 Skill 共同注册到 OMP 原生目录，系统上下文只展示名称、分类与用途描述，不再预先注入完整正文。Agent 根据当前任务选择适用项，通过读取工具访问 `skill://名称` 后才把该正文带入对话。用途描述应写清适用条件；不适用的正文不需要读取。append/replace 两种提示词模式与未自定义系统提示词均使用相同机制。停用项不注册，正文不进行变量替换。

每项正文写为私密原生 `SKILL.md`（name/description frontmatter），description 加上分类标签，设置中的原始描述保持不变。经本轮专用 `--config` overlay 的 `skills.customDirectories` 接入 OMP，由原生提示构建器生成索引与读取指引；Runtime 不重复生成另一份全文或目录提示。UI 的 `/skills` 是管理入口，不是 OMP 原生 `/skill:` 的直接转发。

OMP 原有的项目/用户 Skill 发现机制仍存在。OMP overlay 会替换数组，因此在需要注册 Paveway Skill 时，启动链路先用同一 workspace/profile/环境执行无模型调用的 `omp config get skills.customDirectories --json`，再将本轮目录追加到已有目录，生成私密 overlay。查询失败明确阻止启动，不以空目录覆盖配置。其他原生 Skill 设置、过滤器和发现开关不被改写，也不持久修改 OMP 设置。

Runtime 的 `agent.skills_enabled=false` 会阻止所有 Paveway 管理的 Skill（内置与三类用户 Skill）注册，并传递 OMP 原生 `--no-skills`；用户配置仍保留。`agent.tools_enabled=false` 时无法按需读取正文，OMP 会在没有可用 Skill 读取工具时隐藏原生索引，正文也不会作为替代注入系统提示。UI 会提示，不会自动打开权限。Skill 是同 UID 用户或项目发布者提供的可信指令，不自动安装插件、执行附带脚本或下载依赖。当前只管理单份 Markdown 正文，不导入外部 Skill 包的脚本/资源目录。

## 第一方 Built-in Skills

内置 Skill 由 Paveway 仓库维护并随 wheel 发布；不存入 `customization.json`，不属于三种用户分类，也不计入 32 项用户 Skill 上限。`/skills` 和 `workflow customize skill-*` 只管理用户记录，不能修改或删除包资源。两种来源复用同一个 `customization.snapshot()`、本轮私密目录和 OMP 原生发现链路，没有第二套生命周期。

在 `workflow_root/<ID>/workflow.toml` 顶层显式启用：

```toml
builtin_skills = ["paveway-tijian"]
```

省略等同 `[]`，未声明的内置 Skill 不进入该 workflow。新建 coach 默认声明 `paveway-tijian`，新建 example 不声明；升级不会自动改动已有 workflow manifest 或 `event.md`。已有 coach 需要管理员显式添加字段；与 MCP 是否连接无关。`workflow show/list` 返回声明名称以供诊断。未知、非法或重复名称会拒绝加载。

官方资源名称使用 `paveway-` 命名空间。用户 Skill 名称规则不变；若声明的内置 Skill 与已有用户记录同名（包括停用记录），派发前明确报冲突，不覆盖、不自动重命名、不迁移或删除用户数据。需要用户显式解决记录或调整声明。全局停用不会绕过 manifest 校验或隐瞒名称冲突。

### 添加下一份内置 Skill

1. 新建 `src/oicoach_runtime/bundled/skills/paveway-<name>/SKILL.md`，目录名为不超过 64 字符的小写字母/数字与短横线名称。
2. 使用标准 YAML frontmatter：`name` 必须与目录名一致，`description` 为非空字符串；其后写非空 Markdown 正文。文件为 UTF-8、无 NUL，最多 64 KiB。不需要单独配置名称、版本或正文注册项。
3. `builtin_skills.registry()` 使用 `importlib.resources` 枚举 wheel 内目录，以 PyYAML 安全读取 frontmatter，并拒绝重复键、非法目录名、缺失/无效资源及重复注册。不依赖 cwd、Git checkout 或可编辑安装。
4. 在需要的 workflow 中声明名称；只有希望改变**新建** workflow 默认值时才修改创建模板。保留已有 workflow 和用户数据。
5. 运行相关测试、真实 wheel 安装及无模型 OMP 验收。`pyproject.toml` 已包含 `bundled/skills/*/SKILL.md`；当前不支持附带脚本或其他资源文件，新增资源类型须显式扩展打包与快照行为。

后续修改正文只需更新这份 `SKILL.md` 并发布新版 Paveway，无须改注册逻辑。资源在派发前读取并完整复制到本轮 0700/0600 快照，之后的用户编辑、声明变化或软件资源升级不改变该副本；下次派发使用届时的软件资源。恢复既有会话仍遵守原 session/revision、dispatch 和 uncertain 约束，不因升级自动重发任务。历史中已读取的正文不被清除。

OMP 只将名称、description 和原生读取指引放进系统上下文；Agent 使用 `read` 读取 `skill://paveway-tijian` 时才取得正文。Runtime 不生成第二份索引、不预先拼接正文，关闭工具时也不全文降级注入。

`coach.md` 仍负责 workflow 的固定教学规则；Tijian Skill 负责业务知识、能力选择及跨工具工作流；当前 MCP Server 的工具说明与 Schema 才是具体 API 契约。Skill 的声明不意味着 Tijian 已连接，不注册 MCP 工具，不把工具目录或 Schema 硬编码进 Runtime。安全、身份和记忆执行约束仍在 Runtime/System Prompt 中，而非依赖按需读取才能生效。


## 生效、并发与历史

保存成功后，下次**派发**请求时读取新设置，无需重启 daemon 或切换 task。已经排队但尚未派发的请求采用派发时版本；已经开始的回合保持其旧快照。同一 task 可继续使用原会话，历史仍会影响模型，并不会因修改/删除提示词或 Skill 而抹除旧对话。升级前已进入上下文的 Skill 正文或之前读取的内容也可能留在旧历史里；按需读取减少之后的预先注入，不清除历史，需完全隔离时可用 `/new` 新建 task。

每轮将设置快照、系统提示词和原生 Skill 文件保存在 `state_dir/sessions/<binding>/customizations/<run_id>/`，目录 0700、文件 0600。快照不会被后续编辑覆盖，作为该回合的审计资料保留；备份与隐私管理应包括这些文件。恢复默认/删除 Skill 不会删除旧快照或原生会话历史。

每次保存携带所读 revision，并在文件锁内检查、原子替换。两个窗口同时编辑时，后保存者得到 conflict，不覆盖新版本；UI 保留表单和输入。Esc 关闭表单后重新打开，核对最新内容再保存。不会强行把已提交的请求重新发送。

此功能沿用 UI → 公开 CLI 子进程的架构，不直接写数据库；无新的 daemon API、DB/config/manifest 版本变更。升级到支持本功能的 wheel 后，需在无活动回合时重启已有 daemon 一次；之后编辑自定义设置不再需要重启。

## CLI

UI 使用的公开命令也可供脚本调用。写入前先 show，取回 `settings.revision`，正文用 UTF-8 文件传递：

```bash
oicoach-runtime workflow customize show coach --config /absolute/config.toml --json
oicoach-runtime workflow customize prompt coach --mode append \
  --content-file /absolute/system.md --expected-revision 0 --config /absolute/config.toml --json
oicoach-runtime workflow customize skill-put coach --name training-review \
  --category personal-skill --description '训练复盘时使用' --content-file /absolute/skill-body.md \
  --expected-revision 1 --config /absolute/config.toml --json
oicoach-runtime workflow customize skill-disable coach --name training-review \
  --expected-revision 2 --config /absolute/config.toml --json
```

`skill-put --category` 可指定三类标识；省略时新建默认 personal-skill，编辑则保留原分类。分类字段随用户配置一起 Git 同步，旧版本的归档记录仍可读取；使用此字段的设备应升级到 0.12.0 或更新版本。

其他写入操作是 `skill-enable`、`skill-delete`，同样要求 `--name`、当前 `--expected-revision`。空文件配合 `prompt --mode append` 可恢复默认。所有操作明确选择 workflow，不修改同 workspace 下其他 workflow 的自定义设置。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p test_customization.py -v
.venv/bin/python -m unittest discover -s tests -p test_tui.py -v
OICOACH_TEST_REAL_OMP_CONFIG=/absolute/test-config.toml PYTHONPATH=src:tests \
  .venv/bin/python -m unittest \
    test_customization_omp.RealCustomizationTests.test_native_builtin_and_user_skills_read_without_model \
    test_customization_omp.RealCustomizationTests.test_native_system_prompt_append_replace_and_skill_discovery_without_model \
    test_customization_omp.RealCustomizationTests.test_disabled_tools_or_skills_do_not_inject_a_fallback_body -v
.venv/bin/python -m unittest discover -s tests -p test_builtin_skills.py -v
```

上述原生验收不发送 prompt，不调用模型 API：从真实 OMP RPC `get_state.systemPrompt` 检查内置/用户/已有外部目录的索引及权限，再用同一快照配置启动真实 `omp read skill://paveway-tijian:raw` 取得完整正文，同时确认 RPC 会话没有消息。读取发生在单独的原生 CLI 进程，不冒充 RPC 内部模型工具调用。测试配置应选择可正常加载现有 context guard 的 OMP 安装，且在隔离用户目录时显式指定可识别的 `agent.model`（只用于启动，不发送模型请求）。独立 ELF 的包依赖解析若不能满足现有 guard，可使用官方 npm 发行包和 Bun 验证，不跳过或伪造 guard。

内置资源的 wheel 验收使用 `pip wheel --no-deps --wheel-dir /tmp/paveway-wheels .` 构建真实 wheel，再设置 `PAVEWAY_TEST_WHEEL=/absolute/oicoach_runtime-...whl` 运行 `test_builtin_skills.py`；测试会创建无可编辑源码的新 venv、安装 wheel 及依赖，在仓库外通过隔离 Python 读取并快照资源。原生内置 Skill 验收见 `test_customization_omp.py` 中无需模型的方法；不要为验证基础设施运行其中的模型付费测试。
