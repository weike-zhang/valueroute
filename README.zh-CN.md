# ValueRoute

[English](README.md) | 简体中文

ValueRoute 是一个独立的 FastAPI 编排服务，回答多代理任务里一个具体的问题：**这个任务该由主控直接做，还是拆给 Worker，拆几个？** 它不替你改代码，只根据任务边界、写入区域和证据，给出带理由、带费用估算的只读建议，并记录每次建议供离线对比。

## 它解决什么问题

长任务里，你不知道任务能不能拆、拆了安不安全：

- 两个模块互不依赖，能不能并行派两个 Worker？强行并行会不会改到同一片区域？
- 新需求算"新增功能"还是"改现有范围"？改范围的任务不能盲目派工。
- 派 Worker 要花多少 token、多少钱、多久？值不值得派？

ValueRoute 把判断变成可核对的规则和证据：它只读入请求包络（envelope），产出边界分类、需求图和候选建议，给出拒绝原因、预计 token、费用和延迟。它从不注册主控、从不创建 WorkerPlan、从不修改模型配置，advisory 模式没有写权限。

## 现在已经能做什么

- 本地 append-only JSONL journal，可恢复、可快照压缩
- 会话/任务/计划注册与确定性校验
- expected-version 与 Idempotency-Key 防重放
- 区域级 Writer Lease：同文件不同符号可并行，重叠区域冲突
- 隔离工作区 + ChangeSet 校验 + 原子集成 + 父级验收
- 0–5 Worker 队列、心跳、事件驱动 Checkpoint、kill -9 恢复
- `/v1/advisory` 只读路由建议，shadow 记录持久化、重启不丢

## 快速开始

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q        # 189 个测试
VALUEROUTE_DATA_DIR=/tmp/valueroute-data python3 -m valueroute.main
```

启动后访问 `/v1/health/live` 和 `/v1/health/ready`，或用 `schemas/v1/` 下的请求 schema 调用 `/v1/advisory` 看一次路由建议。

## 证据

- [验收矩阵](docs/acceptance-matrix.md)：设计文档 §23 的 36 项验收全部勾选，每项对应实现与测试
- [离线评估集](evaluation/)：三个冻结任务族 × 5 个任务，ground truth 标注派工期望，harness 用真实模型跑 A/B/C 三组对比（token、费用、延迟）
- [首次真实运行](docs/evaluation.md)（2026-08-15，gpt-5-6-mini）:15 个任务中派工决策 6 正确、5 过度派工、4 应派未派;A/B/C 三组通过 4/7/5，费用约 $0.019 / $0.035 / $0.037
- [NFR 与供应链证据](docs/nfr-and-supply-chain-evidence.md):journal 性能、SBOM、依赖锁

诚实边界：评估的验收是关键词判定，不是完整任务执行;`quality_claim： false`。README 里的性能数字都能追溯到 `evaluation/evidence/` 下的原始结果文件。

## 限制

- 只做本地单进程编排;数据库、Redis、远程状态服务都不需要，也没有接入
- advisory 只建议，**自动派工保持关闭**，直到离线评估集证明质量、费用或延迟收益
- 生产级认证、远程部署加固不在本地优先发布的证据范围内

## 文档

- [架构](docs/architecture.md)、[领域模型](docs/domain-model.md)、[API 规格](docs/api-spec.md)
- [所有权与区域租约](docs/ownership-and-region-lease.md)、[检查点与恢复](docs/checkpoint-and-recovery.md)
- [测试哲学](docs/testing-philosophy.md)、[评估](docs/evaluation.md)
- [安全](SECURITY.md)、[贡献指南](CONTRIBUTING.md)

## 仓库

- 主页:<https://github.com/weike-zhang/valueroute>
- 变更日志：[CHANGELOG.md](CHANGELOG.md)
- 许可证：[LICENSE](LICENSE)
