"""impact_score.py — 政策影响评分算法

根据用户画像（企业类型、关注省份、业务阶段）和政策属性
（地区、层级、状态、影响对象、分类）计算匹配度（0-100 分）。
"""

from __future__ import annotations
import json

# ── 常量：供前端下拉选项使用 ──────────────────────────────────────────────────

COMPANY_TYPES = [
    "电站开发商",
    "储能系统集成商",
    "设备制造商",
    "电网公司",
    "检测认证机构",
    "运维单位",
]

BUSINESS_STAGES = ["规划", "建设", "运营", "全阶段"]

PROVINCES_LIST = [
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "内蒙古", "广西", "西藏", "宁夏", "新疆",
]

# ── 匹配规则 ──────────────────────────────────────────────────────────────────

# 企业类型 → 政策 impact_on 字段关键词
_COMPANY_IMPACT_KEYWORDS: dict[str, list[str]] = {
    "电站开发商":     ["电站开发商", "投资方", "开发商", "独立储能", "共享储能", "项目业主"],
    "储能系统集成商": ["系统集成", "集成商", "EPC", "总承包"],
    "设备制造商":     ["设备制造商", "制造商", "设备厂商", "电池厂商", "PCS", "BMS", "EMS"],
    "电网公司":       ["电网公司", "调度机构", "电网", "并网", "调度", "电力调度"],
    "检测认证机构":   ["检测认证", "认证机构", "检测机构", "第三方检测", "认证"],
    "运维单位":       ["运维单位", "运维", "O&M", "维护", "运营维护"],
}

# 业务阶段 → 相关政策分类
_STAGE_CATEGORY_MAP: dict[str, list[str]] = {
    "规划": ["政策/规划/指导意见", "补贴资金/奖补", "示范项目/试点清单", "招标公告"],
    "建设": ["标准规范", "验收/检测/认证", "安全消防", "通知/办法/细则/实施方案", "并网与调度"],
    "运营": ["市场规则", "并网与调度", "通知/办法/细则/实施方案", "事故通报/风险预警/执法处罚"],
    "全阶段": [],  # 匹配所有
}


def _parse_list(v) -> list[str]:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []


def calculate_impact_score(user_profile: dict, policy: dict) -> dict:
    """计算政策对用户的影响评分。

    Args:
        user_profile: {company_type, provinces (list|JSON str), business_stage}
        policy: {region, level, status, categories, tags, impact_on}

    Returns:
        {score: int(0-100), level: str, explanation: str, dimensions: dict}
    """
    if not user_profile:
        return {
            "score": 0,
            "level": "未知",
            "explanation": "请先设置企业画像以获取个性化评分",
            "dimensions": {},
        }

    company_type   = (user_profile.get("company_type") or "").strip()
    user_provinces = _parse_list(user_profile.get("provinces", []))
    business_stage = (user_profile.get("business_stage") or "全阶段").strip()

    policy_region     = (policy.get("region") or "").strip()
    policy_level      = (policy.get("level") or "").strip()
    policy_status     = (policy.get("status") or "现行").strip()
    policy_categories = _parse_list(policy.get("categories", []))
    policy_impact_on  = _parse_list(policy.get("impact_on", []))

    score   = 0
    reasons: list[str] = []
    dimensions: dict[str, int] = {}

    # ── 维度1：地区匹配（30 分）──────────────────────────────────────────────
    if policy_level == "国家":
        region_score = 30
        reasons.append("国家级政策，全国适用")
    elif policy_region and user_provinces and policy_region in user_provinces:
        region_score = 30
        reasons.append(f"关注地区精准匹配（{policy_region}）")
    elif policy_region and user_provinces:
        region_score = 5   # 不在关注省份
    else:
        region_score = 12  # 未设置关注省份给基础分
    dimensions["region"] = region_score
    score += region_score

    # ── 维度2：企业类型匹配（35 分）─────────────────────────────────────────
    if company_type:
        keywords   = _COMPANY_IMPACT_KEYWORDS.get(company_type, [])
        impact_str = " ".join(policy_impact_on)
        matched    = [kw for kw in keywords if kw in impact_str]
        if matched:
            company_score = 35
            reasons.append(f"影响对象包含{company_type}")
        elif policy_level == "国家":
            company_score = 20
        else:
            company_score = 5
    else:
        company_score = 15  # 未设置企业类型
    dimensions["company_type"] = company_score
    score += company_score

    # ── 维度3：业务阶段匹配（20 分）─────────────────────────────────────────
    if business_stage == "全阶段" or not business_stage:
        stage_score = 20
    else:
        relevant_cats = _STAGE_CATEGORY_MAP.get(business_stage, [])
        matched_cats  = [c for c in policy_categories if c in relevant_cats]
        if matched_cats:
            stage_score = 20
            reasons.append(f"政策类型与{business_stage}阶段匹配")
        else:
            stage_score = 8
    dimensions["business_stage"] = stage_score
    score += stage_score

    # ── 维度4：政策层级与状态加成（15 分）───────────────────────────────────
    if policy_level == "国家":
        imp_score = 12
    elif policy_level in ("省", "市"):
        imp_score = 9
    else:
        imp_score = 5
    if policy_status == "征求意见":
        imp_score = min(imp_score + 3, 15)
        reasons.append("正在征求意见，可参与反馈")
    dimensions["importance"] = imp_score
    score += imp_score

    score = min(score, 100)

    if score >= 80:
        level_text = "高度相关"
    elif score >= 55:
        level_text = "中度相关"
    elif score >= 30:
        level_text = "低度相关"
    else:
        level_text = "关联性弱"

    explanation = f"{level_text}（{score}分）"
    if reasons:
        explanation += "：" + "；".join(reasons)

    return {
        "score": score,
        "level": level_text,
        "explanation": explanation,
        "dimensions": dimensions,
    }
