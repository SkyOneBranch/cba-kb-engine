> [!WARNING]
> **SUPERSEDED HISTORICAL RECORD**: This document is an archived historical specification/record and is no longer active. It has been sanitized for public repository compliance.

# MASTER 载体迁移核对（历史 v1.1 迁移快照）

> [!NOTE]
> 以下 `3451` 行统计系 **`2026-09-10`** 早期 v1.1 载体迁移期间的只读核对快照（已属历史迁移记录）；当前生产 `MASTER` 行数由发版构建期动态统计并写入权威消费面导览与字段字典（当前不再以静态 `3,451` 作为实时总行数）。

2026-09-10 只读比较：归档 <DRIVE_ID>（677658字节、43列）与当前 <DRIVE_ID>（314762字节、20列）。两者均3451行，record_key集合相同。

直接映射：registration_type→registration_stage；former_cba_club→former_club；disclosure_deadline→notice_deadline；notes→remarks。其他同名字段在比较范围内一致，除sequence由数值变字符串、32行verification_level由official_roster变auto_validated。此为既有v1.1生产基线，v1.5未再次修改。

不在20列中的ISO日期、raw_row_text、secondary_source_ref等保存在旧43列归档，不宣称20列涵盖旧表全部信息。旧归档需长期保留，必要时按record_key回查。32行旧official_roster不属于当前核验词表；当前auto_validated仅按现有基线保留，不能据旧标签提升为人工/官方核验。v1.5不据工程审查重写事实核验等级；后续如需调整，须逐行来源审查与显式数据发布。

字节SHA验证传输与精确恢复；semantic SHA验证表格内容，两者分别记录。builder只读源XLSX，不重复保存源文件。v1.5正式发布保持MASTER原ID与原字节。
