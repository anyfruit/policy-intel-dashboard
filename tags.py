"""tags.py — 轻量规则引擎（无外部依赖）

目的：
- 给抓取脚本提供：分类/标签/影响对象/状态/层级的粗粒度推断
- 给后端提供：CATEGORY_TYPES 常量（与前端保持一致）

注意：这里是「规则优先」的启发式，不追求 100% 精准，追求：
1) 不漏掉储能/电力市场相关内容
2) 能把明显噪音（油价/社会信用/垃圾等）先挡住（在 cleanup 里会二次处理）

"""

from __future__ import annotations

import re
from typing import List


CATEGORY_TYPES = [
    "政策/规划/指导意见",
    "通知/办法/细则/实施方案",
    "市场规则",
    "标准规范",
    "并网与调度",
    "安全消防",
    "验收/检测/认证",
    "补贴资金/奖补",
    "示范项目/试点清单",
    "招标公告",
    "中标结果/候选人公示",
    "事故通报/风险预警/执法处罚",
]


# ─────────────────────────────────────────────────────────
# 关键词库
# ─────────────────────────────────────────────────────────

_STORAGE_KWS = [
    "储能",
    "新型储能",
    "电化学",
    "锂电",
    "液流",
    "钠电",
    "压缩空气",
    "飞轮",
    "抽水蓄能",
    "源网荷储",
    "虚拟电厂",
    "调峰",
    "调频",
    "辅助服务",
    "电力市场",
    "现货",
    "容量",
    "两部制",
    "并网",
    "接入",
    "调度",
    "储能电站",
    "独立储能",
    "共享储能",
    "工商业储能",
    "BESS",
    "PCS",
    "BMS",
    "EMS",
]


def _norm(text: str) -> str:
    return (text or "").strip()


def _contains_any(text: str, kws: List[str]) -> bool:
    t = _norm(text)
    if not t:
        return False
    tl = t.lower()
    for k in kws:
        if not k:
            continue
        if k.lower() in tl:
            return True
    return False


# ─────────────────────────────────────────────────────────
# 分类推断
# ─────────────────────────────────────────────────────────

def infer_categories(text: str) -> List[str]:
    """从标题/摘要推断 1-3 个分类。"""
    t = _norm(text)
    out: List[str] = []

    def add(cat: str):
        if cat in CATEGORY_TYPES and cat not in out:
            out.append(cat)

    # 招标/中标
    if re.search(r"招标|采购|询价|竞争性磋商|比选|谈判", t):
        add("招标公告")
    if re.search(r"中标|成交|候选人|结果公示|定标", t):
        add("中标结果/候选人公示")

    # 补贴/试点
    if re.search(r"补贴|奖补|补助|资金|贴息|支持资金|奖励资金", t):
        add("补贴资金/奖补")
    if re.search(r"示范|试点|清单|遴选|申报|征集", t):
        add("示范项目/试点清单")

    # 市场规则
    if re.search(r"市场规则|现货|辅助服务|交易|结算|容量补偿|两部制|峰谷|需求响应", t):
        add("市场规则")

    # 并网与调度
    if re.search(r"并网|接入|调度|运行管理|功率控制|AGC|AVC", t):
        add("并网与调度")

    # 标准/验收/消防/事故
    if re.search(r"标准|规范|规程|指南|GB/?T|DL/?T|NB/?T|团体标准|地方标准", t, re.IGNORECASE):
        add("标准规范")
    if re.search(r"验收|检测|认证|型式试验|抽检", t):
        add("验收/检测/认证")
    if re.search(r"消防|安全|防火|爆炸|热失控", t):
        add("安全消防")
    if re.search(r"事故|通报|处罚|执法|风险预警|整改", t):
        add("事故通报/风险预警/执法处罚")

    # 默认：通知/办法 vs 政策/规划
    if not out:
        if re.search(r"通知|办法|细则|实施方案|规定|管理办法|暂行", t):
            add("通知/办法/细则/实施方案")
        else:
            add("政策/规划/指导意见")

    # 一般情况下避免太多分类
    return out[:3] if out else ["政策/规划/指导意见"]


# ─────────────────────────────────────────────────────────
# 标签（用于 dashboard 趋势）
# ─────────────────────────────────────────────────────────

def extract_tags(text: str) -> List[str]:
    """抽取少量可解释的标签，主要用于趋势图与快速筛选。"""
    t = _norm(text)
    tags: List[str] = []

    def add(x: str):
        if x and x not in tags:
            tags.append(x)

    if _contains_any(t, ["储能", "新型储能", "电化学", "BESS", "PCS", "BMS", "EMS"]):
        add("储能主题")
    if _contains_any(t, ["新能源", "风电", "光伏", "分布式"]):
        add("新能源")
    if _contains_any(t, ["电力市场", "现货", "辅助服务", "交易", "结算", "容量"]):
        add("电力市场")
    if _contains_any(t, ["并网", "接入", "调度", "运行管理"]):
        add("并网")
    if _contains_any(t, ["锂电", "电池", "PACK", "电芯"]):
        add("锂电")
    if _contains_any(t, ["虚拟电厂", "聚合", "负荷聚合"]):
        add("虚拟电厂")
    if _contains_any(t, ["调峰", "调频", "AGC", "辅助服务"]):
        add("调峰调频")
    if _contains_any(t, ["补贴", "奖补", "补助", "资金", "贴息"]):
        add("补贴")
    if _contains_any(t, ["招标", "采购", "中标", "成交"]):
        add("招标")
    if _contains_any(t, ["标准", "规范", "GB", "DL/T", "NB/T", "团体标准"]):
        add("标准")

    # 上限，避免 tag 爆炸
    return tags[:8]


# ─────────────────────────────────────────────────────────
# 影响对象
# ─────────────────────────────────────────────────────────

def extract_impact_on(text: str) -> List[str]:
    t = _norm(text)
    out: List[str] = []

    def add(x: str):
        if x and x not in out:
            out.append(x)

    if re.search(r"项目|电站|开发|备案|核准|并网投运", t):
        add("电站开发商")
    if re.search(r"系统集成|集成商|EMS|EPC", t, re.IGNORECASE):
        add("储能系统集成商")
    if re.search(r"电池|PCS|BMS|逆变器|变流器|PACK|电芯", t, re.IGNORECASE):
        add("设备制造商")
    if re.search(r"电网|国网|南网|调度|并网", t):
        add("电网公司")
    if re.search(r"检测|认证|验收|型式试验|抽检", t):
        add("检测认证机构")
    if re.search(r"运维|运行维护|巡检", t):
        add("运维单位")
    if re.search(r"消防|防火|热失控", t):
        add("消防部门")

    return out[:6]


# ─────────────────────────────────────────────────────────
# 状态/层级
# ─────────────────────────────────────────────────────────

def infer_status(text: str) -> str:
    t = _norm(text)
    if re.search(r"征求意见|公开征求|意见稿", t):
        return "征求意见"
    if re.search(r"废止|失效|停止执行", t):
        return "废止"
    return "现行"


def infer_level(source_id: str, title: str) -> str:
    """粗略推断政策层级：国家/省/市/行业。

    规则：
    - 国家部委/国务院/国家能源局/国家发改委 → 国家
    - 省/自治区/直辖市关键词 → 省/市
    - 行业协会/标准团体 → 行业
    """
    sid = (source_id or "").lower()
    t = _norm(title)

    if re.search(r"国务院|国家发展改革委|国家发改委|国家能源局|国家能源局", t) or any(k in sid for k in ["ndrc", "nea", "gov", "nation"]):
        return "国家"

    if re.search(r"协会|学会|联盟|团体标准", t) or any(k in sid for k in ["cnesa", "cec", "industry", "assoc"]):
        return "行业"

    # 省级/自治区
    if re.search(r"省|自治区|内蒙古|新疆|宁夏|广西|西藏", t):
        return "省"

    # 市级
    if re.search(r"市人民政府|市发改委|市发展改革委|市能源局", t):
        return "市"

    # 默认
    return "国家"


# ─────────────────────────────────────────────────────────
# 相关性：给 cleanup/抓取过滤用
# ─────────────────────────────────────────────────────────

def is_storage_relevant(text: str) -> bool:
    """是否明显与储能/电力市场相关（用于噪音过滤）。"""
    return _contains_any(text, _STORAGE_KWS) or _contains_any(text, ["电力", "新能源", "市场规则", "并网", "调度"]) 
