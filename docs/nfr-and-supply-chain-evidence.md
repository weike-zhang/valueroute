# NFR 与供应链证据

仓库提供一个无外网依赖的、可重复运行的证据采集器。它不启动模型、不调用 provider，也不需要下载任何包。

```bash
python scripts/nfr_evidence.py benchmark --iterations 200 --output /tmp/valueroute-nfr.json
python scripts/nfr_evidence.py sbom --output /tmp/valueroute-sbom.json
python scripts/api_lease_performance.py --iterations 200 --output /tmp/valueroute-api-lease.json
```

脚本本身就是命令入口；从仓库根目录运行即可。它没有被注册为安装后的
`valueroute` 子命令，避免把 `scripts/` 工具误当作运行时包的一部分。

`benchmark` 测量本地 checksummed journal 的追加写入和重放，输出 workload、p50/p95/max、吞吐、重放耗时、Python/平台和源码指纹。`sbom` 输出当前 Python 环境中可见的已安装 distribution 名称与版本，便于审计运行环境；它是 CycloneDX-compatible inventory，不是签名发布物，也不是漏洞扫描结果。

`api_lease_performance.py` 在隔离临时目录中重复调用 `GET /v1/health/live`，并对同一资源重复申请重叠 WriterLease，输出两类工作负载的 p50/p95/max。后者还要求每次冲突都被 `lease_overlap` 拒绝；任何一次错误放行都会以非零状态失败。脚本使用进程内 TestClient 和 LeaseManager，结果只代表当前机器、当前 Python 环境和这组样本，不代表生产 HTTP 网络、并发、多进程或外部数据库性能。

## 证据边界

这些数字只描述运行命令时的本机、Python 版本、磁盘和指定 workload。它们不是生产 SLO、容量承诺或 provider 延迟结论；不同机器、磁盘、进程竞争、数据规模和配置都需要重新采集。提交报告时应保留完整 JSON、命令、代码版本指纹和环境信息，并与全量测试结果一起审阅。

脚本故意不设置“通过/失败”阈值：当前设计尚未定义可公开承诺的 NFR 基线。供应链清单同样不替代依赖锁文件、签名制品、漏洞扫描或发布流程。
