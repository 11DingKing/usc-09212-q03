"""示例规则包：中国与东盟 AI 应用共享场景。

规则为教学/演示用的领域化条款，不构成法律意见；部署时应以受控
环境中的正式规则包替换。规则包一旦发布即不可变，更新请提升版本号。
"""

SEED_PACKAGE = {
    "version": "2026.09",
    "name": "中国—东盟 AI 应用共享基线规则",
    "jurisdictions": {
        "CN": {
            "name": "中华人民共和国",
            "rules": [
                {
                    "id": "CN-DATA-EXPORT-01",
                    "title": "重要数据/个人信息出境安全评估",
                    "detail": "在境内收集的个人信息或重要数据向境外提供前，"
                              "须完成数据出境安全评估或标准合同备案。",
                    "when": {
                        "subject_jurisdictions_any": ["CN"],
                        "cross_border": True,
                        "data_categories_any": [
                            "personal_information", "important_data"],
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["data_export_security_assessment"],
                        "required_roles": ["data_compliance_officer"],
                        "requires_certificate": {
                            "scope": "cn_cross_border_data_transfer"},
                    },
                },
                {
                    "id": "CN-MINORS-01",
                    "title": "未成年人个人信息保护专项规则",
                    "detail": "处理不满十四周岁未成年人个人信息须取得监护人"
                              "同意，并制定专门处理规则。",
                    "when": {
                        "deployment_in": ["CN"],
                        "involves_minors": True,
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["guardian_consent",
                                     "minors_processing_policy"],
                        "required_roles": ["privacy_officer"],
                    },
                },
                {
                    "id": "CN-ALGO-REC-01",
                    "title": "算法推荐与深度合成备案",
                    "detail": "具有舆论属性或社会动员能力的算法推荐服务须"
                              "履行算法备案与安全评估义务。",
                    "when": {
                        "deployment_in": ["CN"],
                        "purposes_any": ["content_recommendation",
                                         "generative_ai"],
                    },
                    "obligation": {
                        "risk_level": "medium",
                        "evidence": ["algorithm_filing_record"],
                        "required_roles": ["data_compliance_officer"],
                    },
                },
            ],
        },
        "SG": {
            "name": "新加坡",
            "rules": [
                {
                    "id": "SG-AI-HIGH-RISK-01",
                    "title": "高风险 AI 决策人工监督与影响评估",
                    "detail": "对个人产生重大影响的自动化高风险决策须开展"
                              "影响评估、提供人工复核渠道并记录决策逻辑。",
                    "when": {
                        "deployment_in": ["SG"],
                        "high_risk_decision": True,
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["ai_impact_assessment",
                                     "human_oversight_procedure"],
                        "required_roles": ["ai_governance_lead",
                                           "business_owner"],
                    },
                },
                {
                    "id": "SG-PDPA-CONSENT-01",
                    "title": "PDPA 同意与告知义务",
                    "detail": "处理个人数据须具备合法同意基础并履行告知义务。",
                    "when": {
                        "deployment_in": ["SG"],
                        "data_categories_any": ["personal_information"],
                    },
                    "obligation": {
                        "risk_level": "medium",
                        "evidence": ["consent_notice"],
                        "required_roles": ["privacy_officer"],
                    },
                },
            ],
        },
        "ID": {
            "name": "印度尼西亚",
            "rules": [
                {
                    "id": "ID-DATA-LOCAL-01",
                    "title": "公共服务数据本地化与出境管控",
                    "detail": "用于公共服务的电子系统运营者须在境内落实数据"
                              "管控措施，数据出境须满足监管要求。",
                    "when": {
                        "deployment_in": ["ID"],
                        "purposes_any": ["public_service",
                                         "content_recommendation"],
                        "cross_border": True,
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["localization_compliance_report"],
                        "required_roles": ["data_compliance_officer"],
                        "requires_certificate": {
                            "scope": "id_data_transfer_approval"},
                    },
                },
                {
                    "id": "ID-MINORS-01",
                    "title": "未成年人数据保护",
                    "detail": "涉及儿童数据的处理须取得监护人同意并设置"
                              "隐私保护默认项。",
                    "when": {
                        "deployment_in": ["ID"],
                        "involves_minors": True,
                    },
                    "obligation": {
                        "risk_level": "medium",
                        "evidence": ["guardian_consent"],
                        "required_roles": ["privacy_officer"],
                    },
                },
            ],
        },
        "MY": {
            "name": "马来西亚",
            "rules": [
                {
                    "id": "MY-HIGH-RISK-01",
                    "title": "高风险 AI 应用伦理审查",
                    "detail": "高风险决策类 AI 系统须经过伦理审查并保留可"
                              "解释性记录。",
                    "when": {
                        "deployment_in": ["MY"],
                        "high_risk_decision": True,
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["ai_impact_assessment"],
                        "required_roles": ["ai_governance_lead"],
                    },
                },
            ],
        },
        "VN": {
            "name": "越南",
            "rules": [
                {
                    "id": "VN-DATA-01",
                    "title": "数据出境影响评估与备案",
                    "detail": "向境外传输个人数据须完成影响评估并向监管机关"
                              "报送备案。",
                    "when": {
                        "deployment_in": ["VN"],
                        "cross_border": True,
                        "data_categories_any": ["personal_information"],
                    },
                    "obligation": {
                        "risk_level": "high",
                        "evidence": ["data_transfer_impact_assessment"],
                        "required_roles": ["data_compliance_officer"],
                    },
                },
            ],
        },
    },
}
