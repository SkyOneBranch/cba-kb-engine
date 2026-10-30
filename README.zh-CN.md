# CBA-KB｜让每一条篮球结论都有出处

[English](README.md) · [5 分钟快速开始](#5-分钟快速开始) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)

**CBA-KB 是一个开源的 CBA 篮球资料整理与研究引擎。** 它帮助研究者和 AI 工具整理球员记录、联赛公告、采访及其他文档，避免把尚未核实的报道当成确定事实。

**最新正式发布的 Engine：以 [GitHub Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases/latest) 为准。**

## 为什么需要 CBA-KB？

假设一篇报道说某球员可能加盟新球队，但官方注册资料仍显示另一支球队。普通搜索摘要容易把传闻说成事实。CBA-KB 帮助区分：

- **来源（Source）：** 记录来自哪里？
- **文档（Document）：** 原始文件或报道究竟写了什么？
- **陈述 / 主张（Statement / Claim）：** 有人说了什么，哪些说法仍待核查？
- **正式事实 / 统计（Fact / Stats）：** 哪些结论有可信记录、精确统计口径支持？

**核心规则：陈述 ≠ 事实；证据 ≠ 事实；未知 ≠ 0。** 回答应保留身份、日期、授权、来源和不确定性，而不是猜测。

## 工作方式

```text
已获授权的来源文件和记录
           ↓
Source / Document / 来源追踪 / 权限
           ↓
身份 · Fact · Stats · Statement / Claim
           ↓
有版本绑定、受授权的查询与证据视图
           ↓
研究者或 AI 消费端核对答案
```

公开仓库提供 **Engine**、数据结构、工具和合成样例。真实研究资料、访问授权与凭证保留在使用者自己的 **Private Instance**。本仓库不包含私人 CBA 语料库。

## 核心能力

| 研究任务 | Engine 提供的能力 |
| --- | --- |
| 整理资料 | 登记来源和文档，识别重复，保留可追溯的元数据 |
| 查找证据 | 搜索已建立索引的内容，并定位来源和文档 |
| 管理 Drive 文件 | 发现变更、制定有界归档/保留计划、执行获授权操作并核验结果 |
| 研究球员 | 区分球员身份、官方事实、来源主张和精确的赛季/球队统计口径 |
| 准备 AI 可读的证据 | 构建版本化查询切片、原文段落、快照/清单绑定及权限核验的消费记录 |

Engine **不提供**托管聊天机器人或实时 HTTP/MCP 查询服务。本地 Engine 负责准备证据；当某个 Google Drive 投影已获授权发布、且使用者可见时，ChatGPT Web 可以通过自身连接器读取该投影。

## 5 分钟快速开始

需要 **Python 3.11+** 和 **Git**，适用于 macOS、Linux 或兼容终端。以下示例只使用公开合成数据，**无需 Google Drive 账号或私人篮球文档**。

```bash
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# 从 GitHub Releases 获取最新正式发布的 Tag。
git checkout "$(python3.11 -c 'import json,urllib.request; print(json.load(urllib.request.urlopen("https://api.github.com/repos/SkyOneBranch/cba-kb-engine/releases/latest"))["tag_name"])')"

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

make doctor
cba-kb --help

# 使用虚构记录运行 clean-room 示例。
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

此示例不会移动或发布任何真实文件。本地 Inbox、索引及获授权的 Drive 操作请参阅[中文操作手册](docs/operations/OPERATIONS.zh-CN.md)。[`main` 分支](https://github.com/SkyOneBranch/cba-kb-engine)可能包含尚未正式发布的代码。

## 产品进展与路线图

### 近期重要里程碑

| 版本 | 主要成果 | 状态 |
| --- | --- | --- |
| v1.5.x–v1.9.0 | 球员注册与导出、文档证据、来源采集、Statement 与 Claim | 历史里程碑 |
| v2.0.2 | 可复用的公开 Engine、许可证、合成夹具和可复现工具 | 历史里程碑 |
| [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) | Inbox 与 Drive 发现、受控归档、文档检索及故障恢复 | 已发布里程碑 |

知识消费组件已在合成资料上进行测试，包括隔离环境中的 ChatGPT Web + Google Drive 查询。这些测试支持开发验证，但不代表对特定私人资料、使用者或生产环境的完整验收。

### 下一步方向

- **有证据的篮球研究：** 让球员身份、事实、统计和存在争议的报道更容易与原始资料对照、比较和核验。
- **可靠的 AI 知识读取：** 通过获授权的工具，更及时、安全地读取研究资料，保留可追溯引用和明确的不确定性。
- **更简单的资料工作流：** 降低采集、整理、更新和检索资料的操作成本，强化隐私边界，优先复用已有 Engine 能力。

以上是方向，不是保证的发布日期。正式变化请查看 [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) 与 [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)。

## 已知限制、授权与安全

- **不能自动升格为 Fact。** Drive 文件可见，或来源出现在清单中，并不能自动证明其每一条正式事实。Fact 来源绑定及跨来源冲突判断仍需审慎验证。
- **同名不等于同一人。** 身份候选必须有当前获授权的身份资料支持；没有证据时保留待澄清，不自动合并球员。
- **Drive 可见性有条件。** Producer 上传不等于 ChatGPT 已看到当前资料；连接器索引、授权及撤权必须按目标和使用时点核验。
- **多用户检索仍有限制。** 部分 Engine 底层搜索入口尚未一致执行逐用户授权（[Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169)）；修复前不要直接将其作为面向不可信多用户的授权边界。
- **合成测试不等于生产验收。** 在使用真实资料前，应针对具体使用者和环境核验权限、来源绑定、新鲜度与实际查询结果。
- **尊重原始资料版权。** 私人阅读和研究权限不自动包含公开转载权限。

## 文档、贡献与许可证

- [中文操作手册](docs/operations/OPERATIONS.zh-CN.md) · [Operations Manual](docs/operations/OPERATIONS.md)
- [参与贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)
- [正式发布](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)
- [数据与许可](DATA_LICENSE.md) · [第三方资料说明](THIRD_PARTY_DATA.md)

**Public Engine** 代码采用 [Apache-2.0](LICENSE)，文档依 [CC BY 4.0](DATA_LICENSE.md)，仓库合成样例依 [CC0-1.0](DATA_LICENSE.md)。第三方文章、照片和其他原始资料仍受原版权所有者保护。私人研究数据和凭证必须保留在使用者独立的 Private Instance 中。

**让篮球研究更容易查证，而不是让 AI 听起来更确定。**
