"""M8 差额说明：把目标分区与基线之间的行数差额拆成可对外解释的条目。

口径要点：
- 原因码是客观事实（迟到修订 / 来源重复 / 改键合并 / 改键撞键重复 / 未解释余量），
  同一行在不同口径下只切换责任方，不切换原因。
- 改键后才撞在一起的重复（KEY_COLLISION）在两种口径下都归平台键治理团队，
  永远不计入来源团队的迟到账，避免来源团队被多记一笔。
- 每个分区：差额 = 基线行数 → 目标行数的实际变化；已解释 = 各条目影响行数之和；
  未解释余量 = 差额 − 已解释，挂值班平台团队补数。
- 差额合计必须与 M6 勾稽的「行数变化」逐数相等，否则拒绝出说明。
"""
import hashlib
import json
import re
from unicodedata import normalize

from app.keys import ReplayError, identifier, nonnegative_count, partition

PLATFORM_KEY_TEAM = "平台键治理团队"
DUTY_TEAM = "值班平台团队"
CALIBERS = ("platform", "source")
CALIBER_NAME = {"platform": "平台口径", "source": "来源口径"}

REASONS = (
    ("LATE_REVISION", "迟到修订"),
    ("DUPLICATE", "来源重复"),
    ("KEY_MERGE", "改键合并"),
    ("KEY_COLLISION", "改键撞键重复"),
    ("UNEXPLAINED", "未解释余量"),
)
REASON_NAME = dict(REASONS)
_KIND_TO_REASON = {
    "revision": "LATE_REVISION",
    "duplicate": "DUPLICATE",
    "merge": "KEY_MERGE",
    "collision": "KEY_COLLISION",
}
SAFE_LIMIT = 2**53 - 1


def signed_count(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("影响行数须为带符号整数")
    token = normalize("NFKC", str(value)).strip()
    if not re.fullmatch(r"-?[0-9]{1,16}|-?[0-9]{1,3}(?:,[0-9]{3}){1,5}", token):
        raise ValueError("影响行数格式无效")
    number = int(token.replace(",", ""))
    if abs(number) > 9_007_199_254_740_991:
        raise ValueError("影响行数超出单条限制")
    return number


def as_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise ValueError("文本证据长度须在 1–120 之间")
    return value.strip()


def split_partition(value):
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError("分区须为 数据集@YYYY-MM-DD")
    name, day_text = value.split("@")
    return partition(name, day_text)


def exact(value):
    """超出 JS 安全整数的金额一律按字符串对外，避免浏览器舍入。"""
    return str(value) if abs(value) > SAFE_LIMIT else value


def basis_points(part, total):
    """带符号基点（1bp=0.01%），向零取整，口径透明可复算；负贡献占比为负。"""
    if total == 0:
        return 0
    scaled = abs(part) * 10000 // abs(total)
    return scaled if part * total > 0 else -scaled


def percent(bps):
    return f"{bps / 100:.1f}%"


def _task_delta(task):
    if task["状态"] != "write" or not task["计划"]:
        return 0
    return task["计划"]["rows"] - (task["当前"] or {}).get("rows", 0)


def parse_baseline(records, task_index):
    """基线行数可由清单显式给出；不给就用任务当前指针。显式值必须与指针一致。"""
    baseline = {}
    for line, entry in enumerate(records or [], 1):
        try:
            key = partition(entry["dataset"], entry["day"])
            rows = nonnegative_count(entry["rows"])
            task = task_index.get(key)
            if task is None:
                raise ValueError("基线分区不在重放范围内")
            current_rows = (task["当前"] or {}).get("rows", 0)
            if rows != current_rows:
                raise ValueError("基线行数与当前指针不一致")
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayError("VARIANCE_BASELINE", f"基线第 {line} 行无法采信：{exc}") from exc
        baseline[key] = rows
    for key, task in task_index.items():
        baseline.setdefault(key, (task["当前"] or {}).get("rows", 0))
    return baseline


def _parse_arrival(raw, line_no):
    try:
        team = identifier(raw["team"])
        source_line = raw["source_line"]
        if type(source_line) is not int or source_line < 1:
            raise ValueError("source_line 须为正整数（来源原始报送行号）")
        source_ref = as_text(raw["source_ref"])
        business_key = as_text(raw["business_key"])
        remapped = raw["remapped"]
        if type(remapped) is not bool:
            raise ValueError("remapped 须为布尔值")
        target_key = as_text(raw.get("target_key") or business_key)
        key = partition(raw["dataset"], raw["day"])
        impact = signed_count(raw["impact_rows"])
        late = raw["arrived_after_freeze"]
        if type(late) is not bool:
            raise ValueError("arrived_after_freeze 须为布尔值")
        original = None
        if raw.get("original_partition") is not None:
            original = split_partition(raw["original_partition"])
        declared = _KIND_TO_REASON.get(raw.get("kind")) if raw.get("kind") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError("VARIANCE_ARRIVAL", f"到达记录第 {line_no} 行证据无效：{exc}") from exc
    return {
        "源行": line_no,
        "来源团队": team,
        "原始行号": source_line,
        "原始记录": source_ref,
        "业务键": business_key,
        "目标键": target_key,
        "分区": key,
        "改键": remapped,
        "影响行数": impact,
        "冻结后到达": late,
        "原始分区": original,
        "申报原因": declared,
    }


def classify(records, task_index):
    """按目标分区 + 目标键分组判定客观原因，不信任来源自报的 kind。"""
    arrivals = [_parse_arrival(entry, line) for line, entry in enumerate(records or [], 1)]
    groups = {}
    for event in arrivals:
        if event["分区"] not in task_index:
            raise ReplayError(
                "VARIANCE_ARRIVAL", f"到达记录第 {event['源行']} 行引用了重放范围外的分区"
            )
        groups.setdefault((event["分区"], event["目标键"]), []).append(event)

    items = []
    for (key, target_key), members in sorted(groups.items()):
        members.sort(key=lambda event: event["源行"])
        any_remap = any(event["改键"] for event in members)
        if any_remap:
            base_reason = "KEY_COLLISION" if len(members) > 1 else "KEY_MERGE"
        else:
            # 同分区同业务键的非改键多条报送：首条是迟到修订，其后全部计来源重复。
            base_reason = "LATE_REVISION" if len(members) == 1 else "DUPLICATE"
        for index, event in enumerate(members):
            reason = "LATE_REVISION" if base_reason == "DUPLICATE" and index == 0 else base_reason
            if reason == "LATE_REVISION" and not event["冻结后到达"]:
                raise ReplayError(
                    "VARIANCE_EVIDENCE",
                    f"到达记录第 {event['源行']} 行不是冻结后到达，不能记为迟到修订",
                )
            if event["申报原因"] and event["申报原因"] != reason:
                raise ReplayError(
                    "VARIANCE_EVIDENCE",
                    f"到达记录第 {event['源行']} 行自报 {event['申报原因']} 与复核 {reason} 不一致",
                )
            items.append({**event, "原因码": reason})
    items.sort(key=lambda event: (event["分区"], event["源行"]))
    return items


def _owners(reason, team):
    """同一行在两种口径下的责任方；原因码不随口径变化。"""
    if reason == "LATE_REVISION" or reason == "DUPLICATE":
        platform = source = team
        charge_platform = charge_source = True
    elif reason == "KEY_MERGE":
        platform, source = PLATFORM_KEY_TEAM, team
        charge_platform, charge_source = False, True
    elif reason == "KEY_COLLISION":
        platform = source = PLATFORM_KEY_TEAM
        charge_platform = charge_source = False
    else:
        platform = source = DUTY_TEAM
        charge_platform = charge_source = False
    return {
        "platform": {"责任方": platform, "计入来源迟到": charge_platform},
        "source": {"责任方": source, "计入来源迟到": charge_source},
    }


def _evidence(event, baseline_rows, target_rows, delta):
    formula = (
        f"目标行数 {target_rows} − 基线行数 {baseline_rows} = 分区差额 {delta}；"
        f"本行影响 {event['影响行数']:+d}"
    )
    return {
        "复算公式": formula,
        "来源团队": event["来源团队"],
        "来源原始行号": event["原始行号"],
        "来源原始记录": event["原始记录"],
        "业务键": event["业务键"],
        "目标键": event["目标键"],
        "改键后归并": event["改键"],
        "原始分区": event["原始分区"] or "—",
        "冻结后到达": event["冻结后到达"],
    }


def _reason_summary(detail, total_delta):
    buckets = {code: 0 for code, _ in REASONS}
    counts = {code: 0 for code, _ in REASONS}
    for event in detail:
        buckets[event["原因码"]] += int(event["影响行数"])
        counts[event["原因码"]] += 1
    rows = []
    for code, name in REASONS:
        bps = basis_points(buckets[code], total_delta)
        rows.append(
            {
                "原因码": code,
                "原因名称": name,
                "条数": counts[code],
                "影响行数": exact(buckets[code]),
                "占比基点": bps,
                "占比": percent(bps),
            }
        )
    return rows


def _owner_summary(detail, total_delta, caliber):
    buckets = {}
    for event in detail:
        owner = event["责任"][caliber]["责任方"]
        record = buckets.setdefault(owner, {"责任方": owner, "条数": 0, "行数": 0})
        record["条数"] += 1
        record["行数"] += int(event["影响行数"])
    rows = []
    for record in sorted(buckets.values(), key=lambda row: row["责任方"]):
        bps = basis_points(record["行数"], total_delta)
        rows.append(
            {
                "责任方": record["责任方"],
                "条数": record["条数"],
                "行数": exact(record["行数"]),
                "占比基点": bps,
                "占比": percent(bps),
            }
        )
    return rows


def build_variance(bundle, tasks, check, plan_id, file_digest):
    task_index = {task["分区"]: task for task in tasks}
    baseline_rows = parse_baseline(bundle.get("baseline"), task_index)
    classified = classify(bundle.get("arrivals"), task_index)

    scoped = sorted(
        set(baseline_rows)
        | {event["分区"] for event in classified}
        | {task["分区"] for task in tasks if task["状态"] == "write"}
    )
    total_delta = sum(_task_delta(task_index[key]) for key in scoped)
    if total_delta != int(check["行数变化"]):
        raise ReplayError(
            "VARIANCE_TIE",
            f"差额合计 {total_delta} 与勾稽行数变化 {check['行数变化']} 不一致，拒绝出说明",
        )

    line_no = 0
    detail, residuals, partition_blocks = [], [], []
    for key in scoped:
        task = task_index[key]
        base = baseline_rows[key]
        target = (task["计划"] or task["当前"] or {}).get("rows", 0)
        delta = _task_delta(task)
        local = [event for event in classified if event["分区"] == key]
        explained = sum(event["影响行数"] for event in local)
        leftover = delta - explained
        lines = []
        for event in local:
            line_no += 1
            owner = _owners(event["原因码"], event["来源团队"])
            row = {
                "说明行号": line_no,
                "分区": key,
                "原因码": event["原因码"],
                "原因名称": REASON_NAME[event["原因码"]],
                "来源团队": event["来源团队"],
                "原始行号": event["原始行号"],
                "原始记录": event["原始记录"],
                "业务键": event["业务键"],
                "目标键": event["目标键"],
                "改键": event["改键"],
                "影响行数": exact(event["影响行数"]),
                "责任": owner,
                "复算证据": _evidence(event, base, target, delta),
            }
            detail.append(row)
            lines.append(row)
        if leftover:
            line_no += 1
            row = {
                "说明行号": line_no,
                "分区": key,
                "原因码": "UNEXPLAINED",
                "原因名称": REASON_NAME["UNEXPLAINED"],
                "来源团队": DUTY_TEAM,
                "原始行号": None,
                "原始记录": None,
                "业务键": None,
                "目标键": None,
                "改键": False,
                "影响行数": exact(leftover),
                "责任": _owners("UNEXPLAINED", DUTY_TEAM),
                "复算证据": {
                    "复算公式": (
                        f"分区差额 {delta} − 已解释合计 {explained} = 未解释余量 {leftover}；"
                        "挂值班平台团队补数后回填本说明"
                    ),
                    "来源团队": DUTY_TEAM,
                    "来源原始行号": None,
                    "来源原始记录": None,
                    "业务键": None,
                    "目标键": None,
                    "改键后归并": False,
                    "原始分区": "—",
                    "冻结后到达": None,
                },
            }
            detail.append(row)
            lines.append(row)
            residuals.append(row)
        partition_blocks.append(
            {
                "分区": key,
                "基线行数": exact(base),
                "目标行数": exact(target),
                "分区差额": exact(delta),
                "已解释合计": exact(explained),
                "未解释余量": exact(leftover),
                "明细行号": [row["说明行号"] for row in lines],
            }
        )

    explained_total = sum(int(row["影响行数"]) for row in detail if row["原因码"] != "UNEXPLAINED")
    residual_total = sum(int(row["影响行数"]) for row in residuals)
    if total_delta != explained_total + residual_total:
        raise ReplayError("VARIANCE_TIE", "明细加未解释余量无法勾回分区差额")

    payload = {
        "批次编号": plan_id,
        "默认口径": "platform",
        "勾稽锚点": {
            "文件摘要": file_digest,
            "原行数": check["原行数"],
            "模拟行数": check["模拟行数"],
            "行数变化": check["行数变化"],
        },
        "守恒校验": {
            "差额合计": exact(total_delta),
            "行数变化": check["行数变化"],
            "明细合计": exact(explained_total),
            "未解释合计": exact(residual_total),
            "差异": exact(total_delta - explained_total - residual_total),
            "勾稽一致": total_delta == int(check["行数变化"])
            and total_delta == explained_total + residual_total,
        },
        "原因汇总": _reason_summary(detail, total_delta),
        "责任汇总": {
            caliber: _owner_summary(detail, total_delta, caliber) for caliber in CALIBERS
        },
        "未解释余量": {
            "条数": len(residuals),
            "行数": exact(residual_total),
            "占比基点": basis_points(residual_total, total_delta),
            "占比": percent(basis_points(residual_total, total_delta)),
            "补数责任方": DUTY_TEAM,
        },
        "口径名称": CALIBER_NAME,
        "分区差额": partition_blocks,
        "明细": detail,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["说明编号"] = "var-" + hashlib.sha256(plan_id.encode() + b":" + canonical).hexdigest()[:24]
    return payload
