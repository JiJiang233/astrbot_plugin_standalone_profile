# STPRO 独立档案（astrbot_plugin_standalone_profile）

让用户创建 OpenAI-compatible 模型档案，并把指定群路由到独立的 AstrBot 原生配置文件。

> 新建档案的 Provider 和原生配置文件统一显示为 `STPRO / QQ号 / 配置名`；旧版 `stpro_<uuid>` Provider 继续兼容。

## 核心原则

- **完整复制插件设置中的“对齐默认配置”** 创建原生配置文件，保留人格、知识库、插件开关等字段，只把默认聊天 Provider 改为档案自己的 STPRO Provider。该设置直接从当前 AstrBot 原生配置列表下拉选择，默认值为 `default`，只影响之后新建的档案。
- **群绑定使用“为特定会话选择配置”**，写入精确的 `完整群 UMO → STPRO 配置文件` 路由，不修改共享 `default`。
- **AstrBot 管理员的显式配置优先级最高**。管理员在 WebUI 中修改 Provider、原生配置文件或群路由后，插件主动让位，不自动改回。
- 插件只操作同时满足两个条件的 Provider：ID 使用新版 `STPRO / ...` 或旧版 `stpro_...` 命名，且存在于插件所有权记录中。

优先级：`管理员显式配置 > STPRO 群配置路由 > AstrBot 当前默认回落`

## 安装前配置

插件直接调用 AstrBot 当前进程中已经存在的 Dashboard 配置服务，创建原生配置文件、装载消息流水线并管理会话配置路由，不需要配置 WebUI 地址或 `abk_` API Key，也不需要修改 AstrBot 源码。新建的 `STPRO / QQ号 / 配置名` 会同时显示在 WebUI 的 Provider 和配置文件列表中，群绑定也会显示在“为特定会话选择配置”中。

## 命令

| 命令 | 说明 | 场景 |
|---|---|---|
| `/stpro help` | 发送按使用频率排列的指令导航图 | 私聊、群聊 |
| `/stpro new <配置名>` | 创建档案并返回模型编号，不自动选择模型 | 私聊 |
| `/stpro set <配置名> endpoint` | 只更新 Endpoint | 私聊 |
| `/stpro set <配置名> apikey` | 只更新 API Key | 私聊 |
| `/stpro set <配置名> all` | 更新 Endpoint 与 API Key | 私聊 |
| `/stpro model <配置名> [模型序号或名称]` | 查看或选择模型 | 私聊 |
| `/stpro list [配置名]` | 查看自己的档案或详情（Key 脱敏） | 私聊 |
| `/stpro remove <配置名>` | 删除档案（需二次确认码） | 私聊 |
| `/stpro bind <配置名> [群号]` | 将群路由到档案对应的原生配置（群聊省略群号） | 私聊、群聊 |
| `/stpro unbind [群号]` | 解绑群 | 私聊、群聊 |
| `/stpro update [配置名]` | 只读可用性检查 | 私聊 |

Endpoint 只需填到域名，插件会自动归一化（不足则补 `/v1`，多带的 `/chat/completions` 会被剥掉），例如：

| 你输入 | 实际使用 |
|---|---|
| `https://api.example.com` | `https://api.example.com/v1` |
| `https://api.example.com/v1/chat/completions` | `https://api.example.com/v1` |
| `https://openrouter.ai/api/v1` | 不改动 |
| `https://api.example.com/v1beta` | 不改动 |

补全结果会在回复里提示。

选模型时可以使用最近一次模型列表中的序号或完整名称：

```
/stpro model <配置名> 1
/stpro model <配置名> gpt-4o-mini
```

序号和名称都只对**最近一次**成功获取的模型列表有效（默认 5 分钟），超时或换配置后需要先执行不带选择参数的 `/stpro model <配置名>` 重新获取。

配置名：中文、字母、数字、`-`、`_`，最长 32 字符，不区分英文大小写；同一用户不能重名，不同用户可以重名。

## 权限语义

- 档案归创建者所有，只有所有者能 `set` / `model` / `remove`。
- 群首次绑定者成为**绑定管理员**，之后只有他能替换或解绑（`bind` 与 `unbind` 都不二次确认）。
- 已有管理员设置的会话 Provider 覆盖或精确配置路由时，`bind` 零写入拒绝。
- 全局 `default` 通配项只是回退规则，不阻止绑定；插件会把目标群的精确路由放到该回退项之前，同时保持其他路由的相对顺序。
- 如果更早命中的是非 `default` 通配会话配置，精确路由写入后会立即回滚，并提示管理员调整路由顺序。
- 插件不监听成员退群或被踢事件。绑定人离群后不会自动解绑，也不会触发机器人退群；原绑定仍由原绑定人管理。
- 管理员接管后标记为 `admin_overridden`，**不会自动恢复**，必须由绑定管理员重新执行 `bind`（且冲突已消失）。

## WebUI 接管行为

管理员在 WebUI 中做了以下任一项，插件都会让位：

1. 修改 STPRO Provider 的 Endpoint / Key / 模型 / 启用状态；
2. 删除该 Provider；
3. 删除 `STPRO / QQ号 / 配置名`，或把该配置的默认聊天 Provider 改成其他 Provider；
4. 删除或改写目标群的精确配置路由；
5. 为目标群增加会话 Provider 覆盖，使其优先于配置文件默认 Provider。

管理员只修改 STPRO 原生配置中的人格、知识库、插件开关等无关字段时，插件不会判定接管，也不会把整份配置覆盖回创建时的副本。

被接管的档案：普通用户的 `set` / `model` 会被拒绝；Provider 仍存在时 `remove` 也会被拒绝（请先在 WebUI 删除 Provider，再执行 `remove` 清理档案）。第一版**不提供解除接管状态的命令**。

## 数据位置

- 插件数据：`<AstrBot 数据目录>/plugin_data/astrbot_plugin_standalone_profile/ownership.json`（另有 `.bak` 备份）
- Provider 的 Endpoint / API Key / 模型 / 启用状态只保存在 AstrBot 原生 Provider 配置中，插件 JSON 不复制这些值。插件 JSON 只补充所有权、原生配置 ID、群绑定和业务状态。

## 监控

在插件配置中设置监控周期（默认 300 秒）与连续失败阈值（默认 3 次）。

- 每个档案每周期只检查一次，再通知其绑定群；
- 首次失败只记录，达到阈值后通知一次，持续失败不刷屏，恢复通知一次；
- 401/403 立即私聊所有者，群内只发脱敏提示；
- 未绑定群时只通知所有者。

## 测试

不需要 AstrBot 运行时和网络：

```bash
# 单元/流程：原生命令参数、原生配置复制、路由安全、管理员优先、掩码与数据最小化（87 项）
python3 data/plugins/astrbot_plugin_standalone_profile/tests/run_tests.py

# 验收：设计文档中的命令、管理员优先、安全与隐私序列
python3 data/plugins/astrbot_plugin_standalone_profile/tests/acceptance.py
```

离线测试已全部通过。真实 AstrBot 环境仍需确认原生配置在 WebUI 可见、会话配置路由展示正确，以及动态配置的 PipelineScheduler 已装载。探针见 `tests/probes.py`。

```python
import data.plugins.astrbot_plugin_standalone_profile.tests.probes as p

await p.run_probes(
    plugin,
    owner_key="aiocqhttp:<用户号>",
    profile_name="<测试配置名>",
    test_group_umo="aiocqhttp:GroupMessage:<测试群号>",
)
```

## 卸载

卸载或停用插件**不会**删除已创建的 Provider、原生配置文件或会话配置路由，只会停止监控并清理内存临时状态。如需彻底清理，请先在每个群执行 `/stpro unbind`，再对每个档案执行 `/stpro remove`。
