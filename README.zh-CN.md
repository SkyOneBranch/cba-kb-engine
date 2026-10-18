# CBA-KB｜让每一条篮球结论都有出处

[English](README.md) · [快速开始](#5-分钟快速开始) · [运维手册](docs/operations/OPERATIONS.zh-CN.md)

**新闻说了什么，不等于事实已经得到证实。** CBA-KB 是面向中国男子篮球职业联赛（CBA）的开源知识库引擎：帮助研究者和开发者整理比赛报道、球员资料、联赛公告与文档，保留来源、身份、版权和核验状态，让人或 AI 查资料时可以追问：**“这条结论依据什么？”**

> **当前发布状态（2026 年 10 月 18 日）：** [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) 是最新的正式 Engine Release。Human 已将本次交付生命周期确认为 **CLOSED，已知限制明确结转后续**。在一个已获授权的私人实例中，真实 Google Drive 资料归位已经执行，一次可回溯原件的私人 Document Consumer 查询也已获 Human 接受。这**不代表**其他安装实例自动取得生产写入权限、不代表所有检索与安全路径均已通过独立验证，也不代表允许公开传播私人资料。
>
> **不同版本身份须分开：** Python 包元数据版本仍为 `2.0.2`；独立的 Drive Data Production Release 仍为 `v2.0.1-1`。`v2.0.4-2` Tag 指向正式发布的 Engine 快照；`main` 可以包含发布后的新提交。

## 为什么需要 CBA-KB？

想象你在研究“某球员是否转会”。一篇新闻可能说“据悉即将加盟”，另一份名单却仍显示原球队。普通搜索容易把两句话混在一起；直接让 AI 总结，更可能把传闻写成确定事实。

CBA-KB 希望把它们分清楚：

- **Source（来源）**：消息来自哪篇文章、哪份公告、哪个文件？
- **Document（文档）**：原件是什么，版本有没有变化，能否回到出处？
- **Statement（陈述）**：来源原文到底说了什么？
- **Claim（主张）**：哪些判断仍然只是待验证的说法？
- **Fact（事实）**：哪些结论已有足够的权威证据和核验记录？

**核心原则：陈述 ≠ 事实；证据 ≠ 事实；未知 ≠ 0。** 当身份、权限或证据不清楚时，系统应保留未决状态或交给人工复核，而不是猜测。

## 它能做什么？

| 场景 | CBA-KB 的作用 | 当前边界 |
| --- | --- | --- |
| 整理研究资料 | 建立来源、文档、球员身份和可追溯记录 | 公开 Engine 已具备相关数据模型与 CLI |
| 检查新闻和观点 | 分离 Statement、Claim、Fact，保留待核验事项 | AI 摘要不会自动变成已核实事实 |
| 本地 Inbox 试用 | 发现 `.txt`、登记 Source/Document、去重、状态查询和关键词检索 | 需要有效的来源和权利元数据；可用下方虚构样例尝试 |
| Google Drive 资料发现 | 发现文件、追踪修订、建立可查询文档索引并生成受控计划 | 需要适当凭据与权限；Engine 直接搜索路径仍有逐主体隔离限制 |
| 安全整理和归档 | 明确计划、人工批准、受控执行、操作后回读 | 已在获授权的私人部署中真实执行；其他部署须自行取得授权 |
| 支持有出处的 AI 阅读 | 保留 Source/Document 身份与原件溯源，供 Consumer 使用 | 已有一次代表性私人 Document 查询获接受；不是公开托管的在线问答服务，也不保证全部 Consumer 能力 |

**CBA-KB 不是聊天机器人，也不是自动抓取全网、自动判定所有篮球事实的服务。** 它是可复用的处理引擎，需要你选择合法数据来源、运行环境和审核流程。

## 产品进展与路线图

### 近期里程碑

- **证据优先的篮球研究（v1.8.x–v1.9.0）：** 建立 Source/Document 证据、球员身份、Statement/Claim、核验队列和研究视图。
- **可复用的公开 Engine（v2.0.2）：** 完成开源代码、许可证、仓库脱敏、合成样本和可复现工具。
- **Drive-backed Inbox-to-Knowledge 流程（v2.0.4-2）：** 增加本地 Inbox 处理、Drive 发现与轮询、受控文件操作及恢复、文档检索改进。Engine 已正式发布，Human 已关闭本次交付周期；获授权的私人实例完成了实际 Drive 归位和 Document Consumer 业务查询验收，已知限制见下文。

### 下一步方向

以下是**待评估的改进方向**，不是已经批准的下一版本范围，也不代表交付时间承诺：

- **基于真实主体的搜索授权：** 修复现有 [Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169) 所跟踪的 Engine 跨主体隔离缺口。
- **更可靠的 Consumer 来源导航：** 让展示的 Document ID 和原件链接始终与权威 Source/Document 映射一致。
- **新增资料的增量可消费能力：** 改善新入库且已获授权的资料进入索引和 Consumer 数据的效率，避免每次重建整批资料。

版本历史见 [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases)，实际问题见 [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)。计划可能调整。

## 5 分钟快速开始

准备：Python **3.11+**、Git；以下命令适用于 Linux/macOS 或兼容的终端。无需任何真实 CBA 私有数据，也无需 Google Drive 凭据。

```bash
# 1. 获取公开仓库
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# 2. 安装到虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

# 3. 环境检查和离线测试
make doctor
make test

# 4. 查看可以使用的命令
cba-kb --help
```

默认克隆的是 **`main`**，可能包含正式发布后合并的修改（例如 PR #191 的中文检索修复）。如需复现 **v2.0.4-2 正式 Engine 发布快照**，请在安装前运行 `git checkout v2.0.4-2`。如需复现 v2.0.2 源码，则切换 `v2.0.2` Tag。包元数据中的 `2.0.2` 不能单独用于判断当前安装的是哪一个源码快照。

### 体验 1：用虚构数据走一次研究流程

```bash
# 避免把私有环境变量混入公开合成测试
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS

# 只使用公开的合成样例，不上传真实内容
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

它会运行仓库中定义的合成数据研究流程。需要分步查看 Source、Statement、Claim、Research View 等命令，可阅读[公开操作手册](docs/operations/OPERATIONS.zh-CN.md)。

### 体验 2：本地 Inbox 自动发现与搜索（开发功能）

```bash
# 运行在仓库外部的独立目录
mkdir -p /tmp/cbakb-demo/config /tmp/cbakb-demo/inbox/documents

cat > /tmp/cbakb-demo/inbox/documents/demo.txt <<'TXT'
Synthetic CBA Bulletin
The Harbor Falcons won a fictional preseason game.
TXT

# 本地 TXT 需要相应的来源和权利声明
cat > /tmp/cbakb-demo/inbox/documents/demo.txt.source.json <<'JSON'
{
  "schema_version": 1,
  "title": "Synthetic CBA Bulletin",
  "origin": "synthetic_fixture",
  "source_url": null,
  "rights": {
    "classification": "private",
    "public_export_allowed": false,
    "evidence": []
  }
}
JSON

# 稳定性检查：间隔后执行第二次扫描
cba-kb --instance-root /tmp/cbakb-demo inbox-scan
sleep 2
cba-kb --instance-root /tmp/cbakb-demo inbox-scan

# 查看状态与关键词检索
cba-kb --instance-root /tmp/cbakb-demo archive-status --all
cba-kb --instance-root /tmp/cbakb-demo archive-query --keyword "Harbor Falcons"
```

这里不会移动、删除或公开你的原始文件。实际处理结果以命令输出为准；若权利声明错误、文件未稳定或需要人工复核，系统可能拒绝摄入。正式使用前请先阅读[完整安全与操作说明](docs/operations/OPERATIONS.zh-CN.md)。

## Google Drive：先确认，再整理

CBA-KB 可以发现、跟踪 Drive 文件，并为归档或保留生成明确计划。**实际移动前需要人工授权，移动后还要核验结果。** 如果文件身份或版本证据不足，就停止操作。

受控移动代码已在 v2.0.4-2 正式发布。项目所有者的私人生产实例后来已执行获授权的资料归位及读回。这**不代表**其他安装实例自动取得生产 Drive 写入权限：每次真实操作仍应遵守所在环境的授权与安全要求。当前私人实例由所有者选择 Manual / On-demand 运行方式，并不强制后台常驻 Worker。OAuth 凭据和私人文件夹信息不得放入公开仓库。

## 已知限制、隐私与安全

- **已知搜索授权缺口：** 当前公开 Engine 的直接搜索/索引路径尚未在所有入口一致地将可信调用者身份与每份文档的 `allowed_principals` 强制匹配。**不要**把这些路径直接暴露给不可信或多用户调用者作为授权边界。缺陷见 [Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169)。本地 OS 权限与私人门户可降低特定部署的暴露风险，但不能证明 Engine 已完整实现逐主体权限隔离。
- **私人阅读不等于公开传播：** 本人已授权的私有资料入库、索引与 AI 辅助阅读，不代表可以公开或再传播原件，仍须遵守来源及版权约束。
- **验收范围有限：** Human 已接受一次具有来源回溯的代表性私人 Document Consumer 查询，但这不等于逐份文档、所有文件格式、权限负例或 Statement/Claim/Fact/Timeline 研究能力均通过独立验证；尚未确定的证据必须如实标记。
- **版本与部署范围：** Engine Release Tag、包版本元数据、私人运行提交、Drive Data Production Release 与 Consumer 数据批次可以分别代表不同的合法状态。一个私人部署成功，不等于所有安装实例自动启用。

## 谁适合使用？

- **篮球研究者 / 数据整理者**：想知道每个结论来自哪里、哪些仍待验证。
- **开发者**：想复用 CBA 数据契约、身份映射、解析工具和合成样例。
- **AI 应用构建者**：不希望模型把传闻、陈述和核验事实混为一谈。

公开仓库提供 **Public Engine**；私人真实资料应由使用者在独立的 **Private Instance** 中依法管理。代码采用 [Apache-2.0](LICENSE)，文档依 [CC BY 4.0](DATA_LICENSE.md)，仓库的合成样例依 [CC0-1.0](DATA_LICENSE.md)。第三方报道、照片等仍受原版权约束，详见[第三方数据说明](THIRD_PARTY_DATA.md)。

## 文档与参与贡献

- [运维手册（中文）](docs/operations/OPERATIONS.zh-CN.md) · [Operations Manual (English)](docs/operations/OPERATIONS.md)
- [贡献指南](CONTRIBUTING.md) · [安全报告](SECURITY.md)
- [数据和版权许可](DATA_LICENSE.md) · [第三方材料说明](THIRD_PARTY_DATA.md)
- [GitHub Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues) · [Pull Requests](https://github.com/SkyOneBranch/cba-kb-engine/pulls)

**我们的目标不是让 AI 说出更确定的话，而是让每个值得相信的篮球结论都能找到证据。**
