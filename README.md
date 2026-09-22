# 跨境算法合规窗

面向中国与东盟机构共享人工智能应用场景的**可独立部署合规判定服务**。
把模型版本、数据类别、用途、部署地与供应商链组成可版本化档案；规则按辖区
版本化发布，更新后自动定位需要重新评估的发布，旧批准永不被追溯篡改。

仅依赖 Python 3.11+ 标准库（HTTP + SQLite），无第三方运行时依赖。

## 能力一览

- **可版本化档案**：发布档案只追加修订；规则包不可变、带内容哈希。
- **规则更新自动定位**：按义务指纹识别受影响发布并入队重评，不波及无关发布。
- **判定不可篡改**：每条判定在 SHA-256 哈希链上只追加，`/audit/chain` 可自校验。
- **利益冲突隔离**：审查者关联机构与供应商链冲突时禁止签署；一人不得兼任两个必要角色。
- **临时豁免**：限定发布 + 辖区 + 义务 + 期限，到期/撤销自动失效。
- **并发签署不跳角色**：必要角色集合逐一签署，数据库约束 + 应用锁防止重复与跳过。
- **外部证明闸门**：证书到期或撤销即时阻断新流量，并安排复评。
- **重启续跑**：评估队列持久化，启动回收中断任务。
- **可复核判定路径**：判定接口返回具体义务、证据缺口（含原因）与逐步判定路径。

## 运行

```bash
# 内存模式（快速试用，重启不保留）
python3 -m service.main --seed --worker --port 8000

# 独立部署：文件数据库持久化
python3 -m service.main --db ./data/compliance.db --seed --worker --port 8000
# 环境变量：HOST / PORT / COMPLIANCE_DB
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 典型流程

```bash
# 1) 注册发布（自动入队首次评估）
curl -X POST localhost:8000/releases -H 'Content-Type: application/json' -d '{
  "id": "rel-1",
  "model_version": "vision-v3.2.1",
  "data_categories": ["personal_information", "biometric_data"],
  "purpose": "credit_scoring",
  "deployment_jurisdiction": "SG",
  "data_subject_jurisdictions": ["SG", "CN"],
  "vendor_chain": [{"name": "CloudHostSG", "jurisdiction": "SG"},
                   {"name": "LabelCoID", "jurisdiction": "ID"}],
  "involves_minors": true,
  "high_risk_decision": true
}'

# 2) 查看判定：具体义务 + 证据缺口 + 判定路径
curl localhost:8000/releases/rel-1/decision

# 3) 补证据 / 登记外部证明 / 授予限期豁免 / 登记审查者关联
curl -X POST localhost:8000/evidence -H 'Content-Type: application/json' -d '{
  "id": "ev-1", "release_id": "rel-1", "kind": "consent_notice",
  "description": "PDPA 同意告知文本"}'
curl -X POST localhost:8000/certificates -H 'Content-Type: application/json' -d '{
  "id": "cert-1", "issuer": "中国国家网信部门",
  "scope": "cn_cross_border_data_transfer",
  "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
  "release_id": "rel-1", "jurisdiction": "CN"}'
curl -X POST localhost:8000/waivers -H 'Content-Type: application/json' -d '{
  "id": "w-1", "release_id": "rel-1", "jurisdiction": "SG",
  "obligation_id": "SG-PDPA-CONSENT-01",
  "scope_note": "监管窗口期同意机制整改，限期 30 天",
  "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z"}'
curl -X POST localhost:8000/reviewers -H 'Content-Type: application/json' -d '{
  "reviewer_id": "alice", "affiliations": []}'

# 4) 必要角色逐一签署（角色冲突 / 兼任 / 重复都会被拒绝）
curl -X POST localhost:8000/releases/rel-1/signoffs -H 'Content-Type: application/json' -d '{
  "assessment_id": 1, "role": "privacy_officer",
  "reviewer_id": "alice", "decision": "APPROVE"}'

# 5) 判定放行；证书到期后该接口自动转为 BLOCKED
curl localhost:8000/releases/rel-1/decision
curl localhost:8000/audit/chain     # 哈希链自检
```

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST/GET | `/releases`，`GET /releases/{id}` | 注册/列出发布档案 |
| POST | `/releases/{id}/revisions` | 追加档案修订 |
| GET | `/releases/{id}/decision` | **发布判定接口**（义务/缺口/路径/流量结论） |
| GET | `/releases/{id}/assessments`，`/assessments/{id}` | 历史判定记录 |
| POST/GET | `/rules`，`GET /rules/{version}` | 发布/查看不可变规则包 |
| POST/GET | `/evidence`，`GET /releases/{id}/evidence` | 证据登记 |
| POST/GET | `/certificates`，`POST /certificates/{id}/revoke` | 外部证明 |
| POST/GET | `/waivers`，`POST /waivers/{id}/revoke` | 临时豁免 |
| POST/GET | `/reviewers`，`GET /reviewers/{id}` | 审查者利益关系登记 |
| POST/GET | `/releases/{id}/signoffs` | 提交/查看签署（GET 需 `?assessment_id=`） |
| GET/POST | `/queue`，`/queue/drain` | 评估队列查看/同步排空 |
| GET | `/audit/chain` | 哈希链完整性自检 |

判定状态机：`BLOCKED → PENDING_SIGNOFF → APPROVED/REJECTED`；
档案修订、规则义务变化或证书/豁免失效时进入 `REQUIRES_REASSESSMENT` 并自动入队。

业务数据与敏感配置应放在受控运行环境；`service/seed_rules.py` 为演示规则，
不构成法律意见，正式部署请通过 `/rules` 发布受控规则包。
