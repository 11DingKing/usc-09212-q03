"""规则匹配与评估引擎。

输入一份档案版本与一份规则集版本，输出：
- decision: APPROVED / CONDITIONAL / BLOCKED
- obligations: 命中规则产生的具体义务
- evidence_gaps: 证据/外部证明缺口（含是否被有效豁免覆盖）
- required_roles / required_certs: 签署与外部证明要求
- trace: 逐条规则的适用与命中轨迹，供审查者复核判定路径
"""
from __future__ import annotations

SEVERITY_BLOCK = "block"
SEVERITY_REVIEW = "review"


def _overlap(a, b):
    return sorted(set(a) & set(b))


def rule_applicable(rule: dict, profile: dict) -> bool:
    """法域过滤：部署地规则、数据出发地的出境规则、以及通用规则。"""
    jurisdiction = rule.get("jurisdiction", "ALL")
    if jurisdiction == "ALL":
        return True
    if jurisdiction == profile.get("deployment_country"):
        return True
    if (jurisdiction == profile.get("data_origin_country")
            and rule.get("theme") == "cross_border_transfer"):
        return True
    return False


def match_conditions(when: dict, profile: dict) -> tuple[bool, list]:
    """逐条评估匹配条件，返回 (是否命中, 判定理由列表)。"""
    reasons = []
    matched = True

    def check(ok: bool, desc: str):
        nonlocal matched
        reasons.append(("✓ " if ok else "✗ ") + desc)
        if not ok:
            matched = False

    categories = profile.get("data_categories", [])
    purposes = profile.get("purposes", [])
    vendors = [v.get("org") for v in profile.get("vendor_chain", [])]
    origin = profile.get("data_origin_country")
    deployment = profile.get("deployment_country")

    if "data_categories_any" in when:
        hit = _overlap(categories, when["data_categories_any"])
        check(bool(hit), f"数据类别需命中其一 {when['data_categories_any']}，命中 {hit}")
    if "data_categories_all" in when:
        missing = sorted(set(when["data_categories_all"]) - set(categories))
        check(not missing, f"数据类别需全部包含 {when['data_categories_all']}，缺少 {missing}")
    if "purposes_any" in when:
        hit = _overlap(purposes, when["purposes_any"])
        check(bool(hit), f"用途需命中其一 {when['purposes_any']}，命中 {hit}")
    if "deployment_country_in" in when:
        check(deployment in when["deployment_country_in"],
              f"部署地 {deployment} 需在 {when['deployment_country_in']}")
    if "deployment_country_not_in" in when:
        check(deployment not in when["deployment_country_not_in"],
              f"部署地 {deployment} 不得在 {when['deployment_country_not_in']}")
    if "data_origin_country_in" in when:
        check(origin in when["data_origin_country_in"],
              f"数据出发地 {origin} 需在 {when['data_origin_country_in']}")
    if "cross_border" in when:
        is_cross = origin is not None and origin != deployment
        check(is_cross == when["cross_border"],
              f"跨境传输判定：出发地 {origin} → 部署地 {deployment}"
              f"（{'是' if is_cross else '否'}跨境）")
    if "vendor_chain_min_length" in when:
        check(len(vendors) >= when["vendor_chain_min_length"],
              f"供应商链长度 {len(vendors)} 需 ≥ {when['vendor_chain_min_length']}")
    if "vendor_org_in" in when:
        hit = _overlap(vendors, when["vendor_org_in"])
        check(bool(hit), f"供应商需命中其一 {when['vendor_org_in']}，命中 {hit}")
    return matched, reasons


def find_exemption(gap: dict, exemptions: list, now: str) -> dict | None:
    """查找覆盖该缺口且仍在有效期内的豁免。豁免按范围精确匹配。"""
    for ex in exemptions:
        if ex["expires_at"] <= now:
            continue
        if (gap["rule_id"] in ex["rule_ids"]
                or gap["id"] in ex["evidence_ids"]
                or gap["id"] in ex["obligation_ids"]):
            return ex
    return None


def apply_exemptions(gaps: list, exemptions: list, now: str) -> list:
    """为每个缺口标注覆盖它的有效豁免（不修改原列表，返回新列表）。"""
    annotated = []
    for gap in gaps:
        ex = find_exemption(gap, exemptions, now)
        annotated.append({**gap, "covered_by": ex["exemption_id"] if ex else None})
    return annotated


def evaluate(profile: dict, ruleset: dict, exemptions: list,
             certificates: list, now: str) -> dict:
    """执行一次合规判定，返回完整可复核结果。"""
    evidence_have = {e.get("id") for e in profile.get("evidence", [])}
    certs_by_type: dict[str, list] = {}
    for cert in certificates:
        certs_by_type.setdefault(cert["cert_type"], []).append(cert)

    obligations, gaps, trace = [], [], []
    required_roles: set = set()
    required_certs: set = set()

    for rule in ruleset["rules"]:
        entry = {
            "rule_id": rule["id"],
            "title": rule.get("title", ""),
            "jurisdiction": rule.get("jurisdiction", "ALL"),
            "theme": rule.get("theme", ""),
            "severity": rule.get("severity", SEVERITY_REVIEW),
        }
        if not rule_applicable(rule, profile):
            entry.update(applicable=False, matched=False,
                         reasons=[f"法域 {entry['jurisdiction']} 对本档案不适用"])
            trace.append(entry)
            continue
        matched, reasons = match_conditions(rule.get("when", {}), profile)
        entry.update(applicable=True, matched=matched, reasons=reasons)
        if matched:
            for ob in rule.get("obligations", []):
                obligations.append({"id": ob, "rule_id": rule["id"],
                                    "theme": rule.get("theme", "")})
            for ev in rule.get("required_evidence", []):
                if ev not in evidence_have:
                    gaps.append({"kind": "evidence", "id": ev, "rule_id": rule["id"],
                                 "severity": rule.get("severity", SEVERITY_REVIEW)})
            for ct in rule.get("required_certs", []):
                required_certs.add(ct)
                held = certs_by_type.get(ct, [])
                if not any(c["expires_at"] > now for c in held):
                    gaps.append({
                        "kind": "cert_expired" if held else "cert_missing",
                        "id": ct, "rule_id": rule["id"],
                        "severity": rule.get("severity", SEVERITY_REVIEW),
                    })
            required_roles |= set(rule.get("required_roles", []))
            entry["obligations"] = rule.get("obligations", [])
        trace.append(entry)

    gaps = apply_exemptions(gaps, exemptions, now)
    uncovered = [g for g in gaps if not g["covered_by"]]
    if any(g["severity"] == SEVERITY_BLOCK for g in gaps):
        decision = "BLOCKED"
    elif gaps:
        decision = "CONDITIONAL"
    else:
        decision = "APPROVED"

    return {
        "decision": decision,
        "obligations": obligations,
        "evidence_gaps": gaps,
        "uncovered_gaps": uncovered,
        "required_roles": sorted(required_roles),
        "required_certs": sorted(required_certs),
        "trace": trace,
    }
