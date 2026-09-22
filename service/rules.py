"""规则包模型与条件求值。

规则包是不可变的版本化档案，结构示例见 seed_rules.py。
每条规则归属于一个司法辖区，条件命中即产生一项义务。
"""
import json

from .errors import ValidationError

SUPPORTED_CONDITION_KEYS = {
    "data_categories_any",
    "purposes_any",
    "deployment_in",
    "subject_jurisdictions_any",
    "vendor_jurisdictions_any",
    "involves_minors",
    "high_risk_decision",
    "cross_border",
}

RISK_LEVELS = {"high", "medium", "low"}


def normalize_package(content):
    """校验并规范化规则包内容，返回规范化后的 dict。"""
    if not isinstance(content, dict):
        raise ValidationError("规则包必须是对象")
    version = content.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValidationError("规则包缺少 version")
    jurisdictions = content.get("jurisdictions")
    if not isinstance(jurisdictions, dict) or not jurisdictions:
        raise ValidationError("规则包缺少 jurisdictions")

    norm = {"version": version.strip(),
            "name": content.get("name", version),
            "jurisdictions": {}}
    for juris_code, juris_body in jurisdictions.items():
        if not isinstance(juris_code, str) or not juris_code.strip():
            raise ValidationError("辖区代码非法")
        if not isinstance(juris_body, dict):
            raise ValidationError(f"辖区 {juris_code} 定义非法")
        rules = juris_body.get("rules")
        if not isinstance(rules, list):
            raise ValidationError(f"辖区 {juris_code} 缺少 rules 列表")
        norm_rules = []
        seen_ids = set()
        for rule in rules:
            norm_rules.append(_normalize_rule(rule, juris_code, seen_ids))
        norm["jurisdictions"][juris_code.strip()] = {
            "name": juris_body.get("name", juris_code),
            "rules": norm_rules,
        }
    return norm


def _normalize_rule(rule, jurisdiction, seen_ids):
    if not isinstance(rule, dict):
        raise ValidationError("规则必须是对象")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValidationError("规则缺少 id")
    if rule_id in seen_ids:
        raise ValidationError(f"规则 id 重复: {rule_id}")
    seen_ids.add(rule_id)

    when = rule.get("when", {})
    if not isinstance(when, dict):
        raise ValidationError(f"规则 {rule_id} 的 when 必须是对象")
    unknown = set(when) - SUPPORTED_CONDITION_KEYS
    if unknown:
        raise ValidationError(f"规则 {rule_id} 含未知条件: {sorted(unknown)}")

    obligation = rule.get("obligation")
    if not isinstance(obligation, dict):
        raise ValidationError(f"规则 {rule_id} 缺少 obligation")
    title = obligation.get("title") or rule.get("title")
    detail = obligation.get("detail", "")
    risk = obligation.get("risk_level", "medium")
    if risk not in RISK_LEVELS:
        raise ValidationError(f"规则 {rule_id} 风险级别非法: {risk}")
    evidence = obligation.get("evidence", [])
    roles = obligation.get("required_roles", [])
    if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
        raise ValidationError(f"规则 {rule_id} 的 evidence 必须是字符串列表")
    if not isinstance(roles, list) or not all(isinstance(x, str) for x in roles):
        raise ValidationError(f"规则 {rule_id} 的 required_roles 必须是字符串列表")
    cert_req = obligation.get("requires_certificate")
    if cert_req is not None:
        if not isinstance(cert_req, dict) or not isinstance(cert_req.get("scope"), str):
            raise ValidationError(
                f"规则 {rule_id} 的 requires_certificate 必须含 scope 字符串")
    return {
        "id": rule_id.strip(),
        "jurisdiction": jurisdiction,
        "title": title,
        "detail": detail,
        "when": when,
        "risk_level": risk,
        "evidence": evidence,
        "required_roles": roles,
        "requires_certificate": cert_req,
    }


def package_hash(content):
    return json.dumps(content, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode().hex()


def build_context(profile):
    """从发布档案构造条件求值上下文。"""
    vendors = profile.get("vendor_chain") or []
    vendor_jurisdictions = set()
    for vendor in vendors:
        if isinstance(vendor, dict):
            juris = vendor.get("jurisdiction")
            if juris:
                vendor_jurisdictions.add(jurisdiction_code(juris))
        elif isinstance(vendor, str) and ":" in vendor:
            vendor_jurisdictions.add(jurisdiction_code(vendor.rsplit(":", 1)[-1]))
    deployment = jurisdiction_code(profile.get("deployment_jurisdiction"))
    subjects = {jurisdiction_code(x) for x in
                (profile.get("data_subject_jurisdictions") or [])}
    cross_border = bool(subjects and deployment not in subjects) or any(
        j != deployment for j in vendor_jurisdictions)
    return {
        "data_categories": set(profile.get("data_categories") or []),
        "purposes": {profile.get("purpose")} if profile.get("purpose") else set(),
        "deployment": deployment,
        "subject_jurisdictions": subjects,
        "vendor_jurisdictions": vendor_jurisdictions,
        "involves_minors": bool(profile.get("involves_minors")),
        "high_risk_decision": bool(profile.get("high_risk_decision")),
        "cross_border": cross_border,
    }


def jurisdiction_code(value):
    if value is None:
        return ""
    return str(value).strip().upper()


def matches(when, ctx):
    """所有出现的条件之间为 AND；未出现的条件不约束。"""
    if "data_categories_any" in when and not (
            ctx["data_categories"] & set(when["data_categories_any"])):
        return False
    if "purposes_any" in when and not (
            ctx["purposes"] & set(when["purposes_any"])):
        return False
    if "deployment_in" in when and ctx["deployment"] not in {
            jurisdiction_code(x) for x in when["deployment_in"]}:
        return False
    if "subject_jurisdictions_any" in when and not (
            ctx["subject_jurisdictions"] &
            {jurisdiction_code(x) for x in when["subject_jurisdictions_any"]}):
        return False
    if "vendor_jurisdictions_any" in when and not (
            ctx["vendor_jurisdictions"] &
            {jurisdiction_code(x) for x in when["vendor_jurisdictions_any"]}):
        return False
    if "involves_minors" in when and ctx["involves_minors"] != bool(
            when["involves_minors"]):
        return False
    if "high_risk_decision" in when and ctx["high_risk_decision"] != bool(
            when["high_risk_decision"]):
        return False
    if "cross_border" in when and ctx["cross_border"] != bool(when["cross_border"]):
        return False
    return True


def applicable_jurisdictions(profile):
    """适用辖区集合：部署地 ∪ 数据主体所在地。"""
    result = {jurisdiction_code(profile.get("deployment_jurisdiction"))}
    result |= {jurisdiction_code(x)
               for x in (profile.get("data_subject_jurisdictions") or [])}
    return {x for x in result if x}


def triggered_rules(package, profile):
    """返回命中的规范化规则列表（仅适用辖区内的规则参与求值）。"""
    ctx = build_context(profile)
    active = applicable_jurisdictions(profile)
    hits = []
    for code, body in package["jurisdictions"].items():
        if jurisdiction_code(code) not in active:
            continue
        for rule in body["rules"]:
            if matches(rule["when"], ctx):
                hits.append(rule)
    return hits
