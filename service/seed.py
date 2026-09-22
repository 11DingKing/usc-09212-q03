"""中国与东盟跨境 AI 应用的基线规则集（v1 种子数据）。

覆盖三条主线：数据出境、未成年人保护、高风险决策；
另含数据本地化与供应商链治理的通用要求。
"""
from __future__ import annotations

RULES_V1 = [
    {
        "id": "CN-XFER-PI",
        "title": "个人信息出境安全评估",
        "jurisdiction": "CN",
        "theme": "cross_border_transfer",
        "severity": "block",
        "when": {"data_categories_any": ["personal", "biometric", "health"],
                 "cross_border": True},
        "obligations": ["ob.cn.security_assessment", "ob.cn.standard_contract"],
        "required_evidence": ["ev.cn.security_assessment_report"],
        "required_certs": ["cert.cac_security_assessment"],
        "required_roles": ["dpo", "legal"],
    },
    {
        "id": "CN-MINOR-CONSENT",
        "title": "未成年人个人信息监护人同意",
        "jurisdiction": "CN",
        "theme": "minor_protection",
        "severity": "block",
        "when": {"data_categories_any": ["minors"]},
        "obligations": ["ob.cn.guardian_consent", "ob.cn.minor_special_rules"],
        "required_evidence": ["ev.cn.guardian_consent_records"],
        "required_roles": ["dpo"],
    },
    {
        "id": "CN-HIGHRISK-DECISION",
        "title": "高风险自动化决策算法备案",
        "jurisdiction": "CN",
        "theme": "high_risk_decision",
        "severity": "block",
        "when": {"purposes_any": ["credit_scoring", "employment_screening",
                                  "biometric_identification"]},
        "obligations": ["ob.cn.algorithm_filing", "ob.cn.human_review_channel"],
        "required_evidence": ["ev.cn.algorithm_filing", "ev.cn.pia_report"],
        "required_roles": ["legal", "model_owner"],
    },
    {
        "id": "SG-PDPA-CONSENT",
        "title": "PDPA 同意与告知义务",
        "jurisdiction": "SG",
        "theme": "data_protection",
        "severity": "review",
        "when": {"data_categories_any": ["personal"]},
        "obligations": ["ob.sg.consent_notification", "ob.sg.dpo_contact"],
        "required_evidence": ["ev.sg.consent_records"],
        "required_roles": ["dpo"],
    },
    {
        "id": "SG-XFER-PI",
        "title": "PDPA 跨境传输相当保护",
        "jurisdiction": "SG",
        "theme": "cross_border_transfer",
        "severity": "block",
        "when": {"data_categories_any": ["personal"], "cross_border": True},
        "obligations": ["ob.sg.comparable_protection"],
        "required_evidence": ["ev.sg.transfer_agreement"],
        "required_roles": ["legal"],
    },
    {
        "id": "MY-XFER-PI",
        "title": "PDPA 跨境传输白名单或同意",
        "jurisdiction": "MY",
        "theme": "cross_border_transfer",
        "severity": "block",
        "when": {"data_categories_any": ["personal"], "cross_border": True},
        "obligations": ["ob.my.transfer_whitelist_or_consent"],
        "required_evidence": ["ev.my.transfer_consent"],
        "required_roles": ["legal"],
    },
    {
        "id": "TH-MINOR-CONSENT",
        "title": "PDPA 未成年人监护人同意",
        "jurisdiction": "TH",
        "theme": "minor_protection",
        "severity": "block",
        "when": {"data_categories_any": ["minors"]},
        "obligations": ["ob.th.guardian_consent"],
        "required_evidence": ["ev.th.guardian_consent_records"],
        "required_roles": ["dpo", "legal"],
    },
    {
        "id": "ID-LOCALIZATION",
        "title": "公共电子系统本地留存",
        "jurisdiction": "ID",
        "theme": "data_residency",
        "severity": "review",
        "when": {"data_categories_any": ["personal"],
                 "deployment_country_in": ["ID"]},
        "obligations": ["ob.id.local_copy"],
        "required_evidence": ["ev.id.hosting_contract"],
        "required_roles": ["security"],
    },
    {
        "id": "VN-LOCALIZATION",
        "title": "网络安全法数据本地化",
        "jurisdiction": "VN",
        "theme": "data_residency",
        "severity": "block",
        "when": {"data_categories_any": ["personal"],
                 "deployment_country_in": ["VN"]},
        "obligations": ["ob.vn.local_storage"],
        "required_evidence": ["ev.vn.local_storage_proof"],
        "required_roles": ["security", "legal"],
    },
    {
        "id": "ALL-VENDOR-CHAIN",
        "title": "供应商链分包清单",
        "jurisdiction": "ALL",
        "theme": "vendor_governance",
        "severity": "review",
        "when": {"vendor_chain_min_length": 3},
        "obligations": ["ob.all.subprocessor_inventory"],
        "required_evidence": ["ev.all.subprocessor_list"],
        "required_roles": ["security"],
    },
    {
        "id": "ALL-HIGHRISK-OVERSIGHT",
        "title": "高风险决策人工监督",
        "jurisdiction": "ALL",
        "theme": "high_risk_decision",
        "severity": "review",
        "when": {"purposes_any": ["credit_scoring", "employment_screening",
                                  "biometric_identification"]},
        "obligations": ["ob.all.human_oversight_log"],
        "required_evidence": ["ev.all.oversight_procedure"],
        "required_roles": ["model_owner"],
    },
]


def seed_if_empty(store, ruleset_id: str = "asean-cn-baseline") -> bool:
    """规则集为空时写入基线版本。返回是否写入。"""
    if store.get_ruleset(ruleset_id) is not None:
        return False
    store.append_ruleset(ruleset_id, RULES_V1)
    store.audit(None, "ruleset.seeded", "ruleset", ruleset_id,
                {"version": 1, "rule_count": len(RULES_V1)})
    return True
