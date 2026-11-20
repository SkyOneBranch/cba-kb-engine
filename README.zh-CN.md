# CBA-KB｜让每一条篮球结论都有出处

[English](README.md) · [5 分钟快速开始](#5-分钟快速开始) · [中文操作手册](docs/operations/OPERATIONS.zh-CN.md)

**CBA-KB 是一个开源的 CBA 篮球资料整理与证据优先研究引擎。** 它帮助研究者和 AI 工具整理球员注册记录、联赛公告、专访及媒体报道，避免把未经核实的传闻或已修订的旧稿当成确定事实。

**最新正式发布的 Engine：[v2.2.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.2.0)（查看全部 [GitHub Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases/latest)）。**

## 为什么需要 CBA-KB？

假设一篇报道称某球员即将加盟新球队，但官方注册总表仍登记在原球队，或者某篇公告发布数小时后悄然修改了正文。普通搜索摘要很容易把传闻或旧稿说成定论。CBA-KB 在架构上严格分层：

- **来源（Source）：** 记录来自哪里、首发源是谁、后续是否发生过同网址正文修订？
- **文档（Document）：** 归档原件或标准 `acquisition_bundle_v1` 采集包究竟写了什么？
- **陈述 / 主张（Statement / Claim）：** 报道主张了什么，哪些说法仍待核查？
- **正式事实 / 统计（Fact / Stats）：** 哪些结论有权威注册记录与精确赛季/阶段统计口径支持？

**核心规则：陈述 ≠ 事实；证据 ≠ 事实；未知 ≠ 0。** 回答必须保留球员身份边界、日期、授权、修订链、来源链接和不确定性，绝不靠猜测填空。

## 工作方式

```text
已获授权的权威记录、官方注册快照与 acquisition_bundle_v1 标准采集包
                               ↓
       DocumentLane 暂存原子归档 · 去重折叠 · 同源修订链追踪
                               ↓
              身份 · Fact · Stats · Statement / Claim
                               ↓
    受影响人物增量重算（单卡外部引用 <=10 条）与 cba-kb ops-sync 对账
                               ↓
       经显式发布切流的自描述消费面视图（Google Drive / 本地投影）
                               ↓
                  研究者或 AI 消费端逐条核对出处
```

公开仓库仅提供确定性 **Engine**（零网络爬虫依赖、零在线 LLM 服务依赖）、数据结构、CLI 工具和合成样例。真实研究语料、外部采集边车、访问授权与凭证严格保留在使用者独立的 **Private Instance** 中，本仓库不包含任何私人 CBA 语料。

## 核心能力

| 研究任务 | Engine 提供的能力 |
| --- | --- |
| 标准化入库与归档 | 原生校验 18 字段 `acquisition_bundle_v1` 采集包与本地文档，支持暂存区原子落盘、单遍近似去重与首发源（`upstream_origin`）同事件折叠 |
| 追踪来源修订历史 | 同一 `canonical_url` 正文变更时保留历史原件并建立双向修订链（驱动 `LOUD_REVISION` 醒目提示），支持官方注册快照离线回放比对 |
| 增量维护人物研究卡 | 自动推导新增/修订文档波及的已批准球员（Dirty-Set）并仅重算受影响卡片；同名歧义严守 `UNDECIDED` 进待审队列，强制单卡外部引用硬上限（`<= 10` 条） |
| 持续运营同步与云盘管控 | 通过 `cba-kb ops-sync` 维护持久检查点并输出生产消费面滞后对账单（`freshness_report.json`），配合统一文件锁保护的 Drive 发现、归档计划与原子发布 |
| 准备 AI 可读的干净证据 | 导出中文自描述 `CURRENT` 核心表、人物研究卡与常规赛/季后赛切片、快照清单绑定及内联链接引用验签（`oracle-scoring-v8`） |

Engine **不提供**后台常驻进程、托管聊天机器人或实时 HTTP/MCP 查询服务，`cba-kb ops-sync` 也**绝不**静默直写生产 Google Drive。本地运行只生成经校验的候选投影与滞后对账报告；唯有操作者显式执行受控发布切流时，才会更新云端消费面。

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

此示例不会移动或发布任何真实文件。本地 Inbox、`cba-kb ops-sync` 增量同步、索引及获授权的 Drive 操作请参阅[中文操作手册](docs/operations/OPERATIONS.zh-CN.md)。[`main` 分支](https://github.com/SkyOneBranch/cba-kb-engine)可能包含尚未正式发布的代码。

## 产品进展与路线图

### 近期重要里程碑

| 版本 | 主要成果 | 状态 |
| --- | --- | --- |
| v1.5.x–v1.9.0 | 球员注册与导出、文档证据、来源采集、Statement 与 Claim | 历史里程碑 |
| v2.0.2 | 可复用的公开 Engine、许可证、合成夹具和可复现工具 | 历史里程碑 |
| [v2.0.4-2](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.0.4-2) | Inbox 与 Drive 发现、受控归档、文档检索及故障恢复 | 历史里程碑 |
| [v2.1.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.0)–[v2.1.1](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.1) | Drive 优先的受控消费入口（`ConsumeSession`）、快照与事实保证链路 | 历史里程碑 |
| [v2.1.3](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.3) | 异常感知人物研究卡投影与独立评分 Oracle | 历史里程碑 |
| [v2.1.4](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.4) / [v2.1.4-1](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.4-1) | 确定性测试与 CI 基座加固、锁耐久性修复、LACD v1.0.4 治理基线对齐 | 历史里程碑 |
| [v2.1.5](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.1.5) | 全盘搜索防污染归档隔离、中文自描述 `CURRENT` 消费面、人物研究卡正式入列与内联引用 Oracle v8 | 已发布里程碑 |
| [v2.2.0](https://github.com/SkyOneBranch/cba-kb-engine/releases/tag/v2.2.0) | Operational KB 持续运营引擎：`acquisition_bundle_v1` 原生契约、同源修订链（`LOUD_REVISION`）、受影响人物增量重算（引用 `<= 10`）、注册表统一文件锁与 `cba-kb ops-sync` 滞后对账 | 最新正式版 |

Engine 代码版本与私有 Google Drive 数据生产面采用解耦双轨制：发布新版本 Engine 仅交付编译器与 CLI 能力，不会自动改写线上消费面，由操作者根据本地滞后对账结果按需发起云端发布切流。

### 下一步方向

- **扩展已核验球员与跨赛季研究覆盖面：** 在严格人工身份消歧的前提下，持续扩充已批准球员映射、人物研究卡与多赛季注册/统计纵向对比。
- **低摩擦的日常积累与按需发版节奏：** 依托外部标准采集输入与本地 `cba-kb ops-sync` 滞后对账单、待审队列摘要，平时低成本本地增量重算，攒够高价值更新后再按需切流上云。
- **持续打磨连接器消费体验与引用核验：** 针对真实 AI 连接器行为（如链接显示标签改写、长对话上下文窗口上限）持续优化消费面呈现与 Oracle 诊断粒度。

以上是演进方向，不是承诺的发布日期。正式变更请查看 [Releases](https://github.com/SkyOneBranch/cba-kb-engine/releases) 与 [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)。

## 已知限制、授权与安全

- **不能自动升格为 Fact。** Drive 文件可见，或入库了 `acquisition_bundle_v1` 报道包，均不能自动证明其中的正式事实或改写 `MASTER` 权威记录；Fact 来源绑定及跨来源冲突仍需审慎验证。
- **同名不等于同一人。** 身份候选必须有获授权的身份证据支持；存在歧义时一律保持 `UNDECIDED` 留入待审队列，绝不按姓名自动合并球员。
- **Drive 可见性有条件。** 本地 `cba-kb ops-sync` 同步与 Producer 上传不等于外部 AI 连接器已读取最新资料；连接器索引、授权及撤权必须按实际环境与时点核验。
- **检索逐 `principal` 授权边界说明。**[Issue #169](https://github.com/SkyOneBranch/cba-kb-engine/issues/169) 已**关闭**（`PR #170` 已在 `drive_search` 与 `drive_search_index` 实现 fail-closed 逐 `principal` ACL 校验）；因底层检索假定 `principal` 来自受信任的调用方身份上下文而不自建鉴权服务，切勿将其直接暴露为未鉴权的公开多用户端点。
- **合成测试不等于生产验收。** 在接入真实资料前，应针对具体使用者和环境独立核验权限、来源绑定、新鲜度与真实客户端查询表现。
- **尊重原始资料版权。** 私人阅读和研究权限不自动包含公开再分发权限。

## 文档、贡献与许可证

- [中文操作手册](docs/operations/OPERATIONS.zh-CN.md) · [Operations Manual](docs/operations/OPERATIONS.md)
- [参与贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)
- [正式发布](https://github.com/SkyOneBranch/cba-kb-engine/releases) · [Issues](https://github.com/SkyOneBranch/cba-kb-engine/issues)
- [数据与许可](DATA_LICENSE.md) · [第三方资料说明](THIRD_PARTY_DATA.md)

**Public Engine** 代码采用 [Apache-2.0](LICENSE)，文档依 [CC BY 4.0](DATA_LICENSE.md)，仓库合成样例依 [CC0-1.0](DATA_LICENSE.md)。第三方文章、照片和其他原始资料仍受原版权所有者保护。私人研究数据和凭证必须保留在使用者独立的 Private Instance 中。

**让篮球研究更容易查证，而不是让 AI 听起来更确定。**
