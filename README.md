# 跨境算法合规窗

面向中国与东盟机构共享 AI 应用场景的**合规判定服务**，可独立部署（纯 Python 标准库，无第三方依赖）。

项目经理把模型版本、数据类别、用途、部署地与供应商链登记为**可版本化档案**；服务依据版本化规则集自动判定某版本能否在目标国家上线，并给出具体义务、证据缺口与可复核的判定路径。

## 运行

```bash
python3 -m unittest        # 运行测试
python3 -m service.main    # 启动服务（默认 127.0.0.1:8000）
```

数据默认持久化到 `./data/compliance.db`（可用 `SERVICE_DB_PATH` 覆盖）。启动时自动写入基线规则集并恢复未完成的评估队列。

## 能力一览

- **可版本化档案**：`PUT /v1/profiles/{id}` 每次写入生成新版本，旧版本不可变。
- **规则更新影响定位**：`POST /v1/rulesets` 追加规则集新版本后，自动 diff 变更规则、定位受影响的发布并标记 `REASSESSMENT_PENDING`、入队复评；旧评估、旧签署、旧发布记录一律不改写。
- **利益冲突隔离**：审查者所属机构或其申报冲突机构出现在档案归属方/供应商链中时，签署被拒绝。
- **临时豁免**：`POST /v1/exemptions` 必须限定范围（规则/证据/义务）且带期限；到期后自动失效，发布时点重新核算。
- **并发签署**：不同角色可并发签署，`(评估, 角色)` 唯一约束防重复；必要角色未齐时发布被拒绝。
- **外部证明门禁**：规则要求的证书缺失或过期时，发布被拒且 `POST /v1/traffic/authorize` 阻断新流量（409）。
- **队列恢复**：评估任务持久化于 SQLite，重启后 `RUNNING` 任务退回 `PENDING` 继续处理。
- **可复核判定**：评估与发布结果均含 `trace`（逐条规则的适用性、命中理由）与 `obligations`、`evidence_gaps`。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/v1/profiles/{id}` | 登记档案新版本 |
| POST | `/v1/rulesets` | 追加规则集版本（触发影响分析） |
| POST | `/v1/evaluations` | 申请评估（异步入队） |
| GET | `/v1/evaluations/{id}` | 查询判定结果与轨迹 |
| POST | `/v1/evaluations/{id}/signatures` | 角色签署（含冲突检查） |
| POST | `/v1/exemptions` | 授予限期豁免 |
| POST | `/v1/certificates` | 登记外部证明 |
| POST | `/v1/releases` | 发布门禁判定（返回义务/缺口/轨迹） |
| POST | `/v1/traffic/authorize` | 新流量准入检查 |
| GET | `/v1/queue` · `/v1/audit` | 队列状态 · 审计流水 |

## 代码结构

- `service/store.py` — SQLite 持久化；审计与发布事件只追加
- `service/engine.py` — 规则匹配与评估引擎
- `service/core.py` — 业务编排（评估、签署、豁免、发布、流量门禁、影响分析）
- `service/worker.py` — 评估队列 worker
- `service/seed.py` — 中国+东盟基线规则（数据出境 / 未成年人 / 高风险决策等）
- `service/main.py` — HTTP 入口
