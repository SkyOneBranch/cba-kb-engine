# CBA-KB｜让每一条篮球结论都有出处

[English](README.md) · [快速开始](#5-分钟快速开始) · [操作手册](docs/operations/OPERATIONS.zh-CN.md)

**CBA-KB 是一个开源的 CBA 篮球资料整理与研究引擎。** 它帮助你整理球员资料、新闻报道、联赛公告和其他文件，让每条信息都能找到来源，方便人和 AI 查询、比较和核实。

**当前正式版本：[v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2)（已发布，已完成本轮交付）。**

## 为什么需要 CBA-KB？

假设你想知道一名球员是否转会。一篇新闻说“可能加盟”，另一份官方名单仍显示原球队。如果只看搜索摘要，很容易把传闻当成事实。

CBA-KB 希望让你分清楚：

- **来源（Source）**：这条消息来自哪里？
- **文档（Document）**：原始报道或文件是什么？
- **陈述（Statement）**：原文究竟说了什么？
- **主张（Claim）**：哪些说法还需要核实？
- **事实（Fact）**：哪些结论有足够证据支持？

**核心原则：陈述 ≠ 事实；证据 ≠ 事实；未知 ≠ 0。** AI 的回答应当有出处，不能把猜测包装成确定事实。

## 它能做什么？

| 你想做的事 | CBA-KB 提供的能力 |
| --- | --- |
| 整理资料 | 登记文件与来源，识别重复内容，保留可追溯记录 |
| 查找内容 | 按关键词查询已建立索引的文档，找到对应来源 |
| 管理 Google Drive 文件 | 发现文件变化，制定归档或保留计划，经授权后执行并核对结果 |
| 研究球员和球队 | 使用身份、陈述、主张和事实等数据模型，区分不同证据的可信程度 |
| 让 AI 辅助阅读 | 为获授权的资料查询提供文档及来源信息，便于核对原文 |

CBA-KB **不是**自动抓取全网、直接宣布篮球事实的聊天机器人。它提供处理资料和追溯证据的工具；真实数据、核验和授权仍由使用者管理。

## 主要版本与当前状态

| 版本 | 主要迭代功能 | 状态 |
| --- | --- | --- |
| **v1.5.x** | 球员注册数据模型、可复现的 Excel 导出、早期 Drive 发布工具 | 历史里程碑 |
| **v1.8.x** | 文档证据、稳定球员身份、消费端核验 | 历史里程碑 |
| **v1.9.0** | 来源入库、Statement、Claim、核验队列和研究视图 | 历史里程碑 |
| **v2.0.2** | 公开开源 Engine、许可证与脱敏、合成测试样例及可复现工具 | 历史里程碑 |
| **v2.0.4-2** | 本地 Inbox、Drive 发现与轮询、受控归档、搜索索引及故障恢复 | **已发布 · 本轮交付完成** |

在项目所有者授权的私人环境中，本轮已经完成实际的 Drive 资料整理，并通过了一次可回溯原件的私人资料问答验收。公开仓库**不包含**这些私人资料。[查看正式版本记录](https://github.com/SkyOneBranch/cba-kb-engine/releases)。

## 5 分钟快速开始

准备好 **Python 3.11+** 和 **Git**。以下命令适用于 macOS、Linux 或兼容终端；使用公开合成数据，**不需要真实 CBA 资料或 Google Drive 账号**。

```bash
# 获取公开代码
git clone https://github.com/SkyOneBranch/cba-kb-engine.git
cd cba-kb-engine

# 使用 v2.0.4-2 正式发布版本
git checkout v2.0.4-2

# 创建独立 Python 环境并安装
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .

# 查看环境和命令
make doctor
cba-kb --help

# 用仓库内的虚构数据体验研究流程
unset CBA_KB_INSTANCE_ROOT CBA_KB_SOURCE_POLICY_JSON CBA_STATS_AES_KEY GOOGLE_APPLICATION_CREDENTIALS
python scripts/run_cleanroom_smoke.py --output .cleanroom/ci-smoke
```

以上示例不会移动或发布你的真实文件。想进一步试用本地 Inbox、关键词检索或 Drive 操作，请阅读[中文操作手册](docs/operations/OPERATIONS.zh-CN.md)。需要最新开发改动时，可以查看 [`main` 分支](https://github.com/SkyOneBranch/cba-kb-engine)；它不一定与已发布的 Tag 完全相同。

## 使用真实数据前要知道

- **操作要授权。** 处理私人文件或移动 Drive 文件时，需要相应访问权限；每次真实文件操作都要确认结果。
- **私人数据不等于公开数据。** 本仓库只有开源代码及合成样例，不提供项目所有者的私人篮球资料。阅读私人资料的许可也不等于可以公开转载。
- **多用户检索仍有限制。** 当前 Engine 底层搜索接口存在待修复的逐用户权限隔离问题（[Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169)）。修复前，不要直接将其作为面向不可信多用户的授权边界。
- **有来源不代表已证实。** AI 摘要和文件中的说法仍需按证据核实。

## 接下来关注什么？

- 完善不同用户之间的检索权限隔离。
- 让资料来源、文档 ID 与原件链接更加一致可靠。
- 让新增资料更方便地进入索引，供已授权的用户查询。

这些是改进方向，**不代表已承诺的下一版本范围或发布日期**。具体进展见 [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)。

## 谁适合使用？

- **篮球研究者和资料整理者**：希望每个结论都能查到出处。
- **开发者**：希望复用数据模型、入库工具和检索能力。
- **AI 应用开发者**：希望 AI 回答有证据，而不是把传闻当事实。

## 文档与贡献

- [操作手册（中文）](docs/operations/OPERATIONS.zh-CN.md) · [Operations Manual (English)](docs/operations/OPERATIONS.md)
- [参与贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)
- [数据与许可](DATA_LICENSE.md) · [第三方资料说明](THIRD_PARTY_DATA.md)
- [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)

公开仓库提供 **Public Engine**；私人真实资料应由使用者在独立的 **Private Instance** 中依法管理。代码采用 [Apache-2.0](LICENSE)，文档依 [CC BY 4.0](DATA_LICENSE.md)，仓库的合成样例依 [CC0-1.0](DATA_LICENSE.md)。第三方报道、照片等仍受原版权约束，详见[第三方数据说明](THIRD_PARTY_DATA.md)。

**让篮球研究更容易查证，而不是让 AI 听起来更确定。**
