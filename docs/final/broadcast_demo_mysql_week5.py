#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盾构播报机器人程序3.0

本程序实现两条播报链路：

1. 环总结播报：读取实时表当前环号，并通过 历史数据可用性联合校验后生成总结。
2. 安全预警播报：仅在实时报警位、目标偏离、数据质量异常或人工复现条件成立时触发。

程序只执行 SELECT 查询，不向数据库写入任何数据。默认连接本机数据库。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymysql


RING_FIELD = "AVBA10"
TIME_FIELD = "localtime1"
ID_FIELD = "id"

HISTORY_FIELDS = [
    "id",
    "localtime1",
    "AVBA10",
    "AVBA02",
    "ATBA02",
    "ATMD02",
    "ATPA21",
    "ATPA22",
    "ATPA23",
    "ATPA24",
    "ATBA03",
    "ACBA03",
    "AVSA01",
    "AVSA02",
    "AVSA11",
    "AVSA12",
]

TARGET_FIELDS = [
    "id",
    "ring_start",
    "ring_end",
    "speed_start",
    "speed_end",
    "torque_start",
    "torque_end",
    "thrust_start",
    "thrust_end",
    "horizontal_segment_pos",
    "vertical_segment_pos",
    "horizontal_pos",
    "horizontal_pos_dist",
    "vertical_pos",
    "vertical_pos_dist",
    "update_date",
]

ALARM_FIELDS = [
    "id",
    "alarm_type",
    "alarm_level",
    "alarm_md5",
    "alarm_field",
    "alarm_status",
    "alarm_start",
    "alarm_end",
    "is_tips",
    "flag_cls",
    "confirm_time",
    "confirm_user",
    "create_time",
    "shield_stop",
    "invalidtime",
    "alarm_subtype",
]

ALARM_LABELS = {
    "T00029": "土压异常",
    "T00030": "刀盘扭矩关注",
    "T00031": "刀盘回转异常",
    "T00037": "滚动异常",
    "T00038": "滚动趋势关注",
    "T00054": "非常停止",
    "T00067": "推进系统关注",
    "T00110": "注浆系统关注",
    "T00142": "其他系统关注",
}

ALARM_KEYWORDS = {
    "土压": ("土压", "压力"),
    "刀盘扭矩": ("扭矩", "刀盘"),
    "总推力": ("推力", "推进"),
    "姿态": ("姿态", "滚动", "俯仰", "偏差"),
    "数据": ("数据", "通讯", "通信", "心跳", "上传", "维护"),
    "停机": ("停止", "停机", "非常停止"),
}


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if math.isnan(number) else number


def to_int(value: Any) -> Optional[int]:
    number = to_float(value)
    return None if number is None else int(round(number))


def fmt_num(value: Any, digits: int = 1) -> str:
    number = to_float(value)
    if number is None:
        return "暂无法评价"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def fmt_value_or_pending(value: Any, suffix: str = "", digits: int = 1) -> str:
    number = to_float(value)
    if number is None:
        return "暂无法评价"
    return f"{fmt_num(number, digits)}{suffix}"


def parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def iso_datetime(value: Any) -> Optional[str]:
    parsed = parse_datetime(value)
    return parsed.isoformat(sep=" ") if parsed else (str(value) if value else None)


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    cleaned = [float(value) for value in values if value is not None]
    return None if not cleaned else sum(cleaned) / len(cleaned)


def valid_values(rows: Sequence[Dict[str, Any]], field_name: str) -> List[float]:
    return [
        number
        for row in rows
        if (number := to_float(row.get(field_name))) is not None
    ]


def window_mean(values: Sequence[float], window_size: int, from_end: bool = False) -> Optional[float]:
    if not values:
        return None
    window = list(values[-window_size:] if from_end else values[:window_size])
    return mean(window)


def relative_deviation(actual: Optional[float], target: Optional[float]) -> Optional[float]:
    if actual is None or target is None:
        return None
    if abs(target) < 1e-12:
        return abs(actual - target)
    return abs(actual - target) / abs(target)


def quote_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"非法表名：{name}")
    return f"`{name}`"


def connect_mysql(args: argparse.Namespace):
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=60,
        write_timeout=60,
    )


def decode_hex_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return bytes.fromhex(str(value)).decode("utf-8", errors="replace").strip()
    except (ValueError, TypeError):
        return str(value).strip()


def normalize_alarm_text(text: str) -> str:
    text = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{1,2}:\d{1,2})?", "", text)
    text = re.sub(r"\d{1,2}时\d{1,2}分(?:\d{1,2}秒)?", "", text)
    text = re.sub(r"第?\s*\d+\s*环", "当前环", text)
    text = re.sub(r"环\s*\d+", "当前环", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,。")
    return text


def is_alarm_active(value: Any) -> bool:
    number = to_float(value)
    if number is not None:
        return number != 0
    text = str(value).strip().lower() if value is not None else ""
    return bool(text and text not in {"0", "false", "null", "none"})


@dataclass
class MetricStats:
    values: List[float]
    start_mean: Optional[float]
    end_mean: Optional[float]
    mean_value: Optional[float]
    min_value: Optional[float]
    max_value: Optional[float]
    valid_count: int
    total_count: int
    zero_count: int = 0

    @property
    def valid_rate(self) -> Optional[float]:
        if self.total_count == 0:
            return None
        return self.valid_count / self.total_count

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_mean": round_or_none(self.start_mean),
            "end_mean": round_or_none(self.end_mean),
            "mean": round_or_none(self.mean_value),
            "min": round_or_none(self.min_value),
            "max": round_or_none(self.max_value),
            "valid_count": self.valid_count,
            "total_count": self.total_count,
            "valid_rate": round_or_none(self.valid_rate, 4),
            "zero_count": self.zero_count,
            "zero_rate": round_or_none(
                self.zero_count / self.total_count if self.total_count else None,
                4,
            ),
        }


def round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def make_metric_stats(
    rows: Sequence[Dict[str, Any]],
    field_name: str,
    window_size: int,
    exclude_zero: bool = False,
) -> MetricStats:
    raw_values = valid_values(rows, field_name)
    zero_count = sum(1 for value in raw_values if abs(value) < 1e-12)
    values = [value for value in raw_values if not exclude_zero or abs(value) >= 1e-12]
    return MetricStats(
        values=values,
        start_mean=round_or_none(window_mean(values, window_size), 3),
        end_mean=round_or_none(window_mean(values, window_size, from_end=True), 3),
        mean_value=round_or_none(mean(values), 3),
        min_value=round_or_none(min(values) if values else None, 3),
        max_value=round_or_none(max(values) if values else None, 3),
        valid_count=len(values),
        total_count=len(rows),
        zero_count=zero_count,
    )


def make_speed_stats(rows: Sequence[Dict[str, Any]], window_size: int) -> MetricStats:
    samples: List[float] = []
    for row in rows:
        sample = mean(to_float(row.get(name)) for name in ("ATPA21", "ATPA22", "ATPA23", "ATPA24"))
        if sample is not None:
            samples.append(sample)
    return MetricStats(
        values=samples,
        start_mean=round_or_none(window_mean(samples, window_size), 3),
        end_mean=round_or_none(window_mean(samples, window_size, from_end=True), 3),
        mean_value=round_or_none(mean(samples), 3),
        min_value=round_or_none(min(samples) if samples else None, 3),
        max_value=round_or_none(max(samples) if samples else None, 3),
        valid_count=len(samples),
        total_count=len(rows),
    )


def summarize_ring(rows: Sequence[Dict[str, Any]], ring_no: int, window_size: int) -> Dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: (
            parse_datetime(row.get(TIME_FIELD)) or datetime.min,
            to_int(row.get(ID_FIELD)) or 0,
        ),
    )
    timestamps = [parse_datetime(row.get(TIME_FIELD)) for row in ordered]
    timestamps = [value for value in timestamps if value is not None]
    mileage = valid_values(ordered, "AVBA02")
    stroke = valid_values(ordered, "ATBA02")

    posture_fields = {
        "horizontal_head": "AVSA01",
        "horizontal_tail": "AVSA11",
        "vertical_head": "AVSA02",
        "vertical_tail": "AVSA12",
    }

    result: Dict[str, Any] = {
        "ring_no": ring_no,
        "record_count": len(ordered),
        "start_time": timestamps[0].isoformat(sep=" ") if timestamps else None,
        "end_time": timestamps[-1].isoformat(sep=" ") if timestamps else None,
        "mileage_start": round_or_none(min(mileage) if mileage else None, 3),
        "mileage_end": round_or_none(max(mileage) if mileage else None, 3),
        "stroke_max": round_or_none(max(stroke) if stroke else None, 3),
        "speed": make_speed_stats(ordered, window_size).as_dict(),
        "total_thrust": make_metric_stats(
            ordered,
            "ATBA03",
            window_size,
            exclude_zero=True,
        ).as_dict(),
        "cutter_torque": make_metric_stats(
            ordered,
            "ACBA03",
            window_size,
            exclude_zero=True,
        ).as_dict(),
        "posture": {
            axis: make_metric_stats(ordered, field_name, window_size).as_dict()
            for axis, field_name in posture_fields.items()
        },
        "missing_core_fields": [
            field_name
            for field_name in ("ATPA21", "ATBA03", "ACBA03", "AVSA01", "AVSA02", "AVSA11", "AVSA12")
            if not valid_values(ordered, field_name)
        ],
    }
    return result


def fetch_latest_realtime(conn, table_name: str) -> Dict[str, Any]:
    table = quote_identifier(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM {table} "
            f"ORDER BY {quote_identifier(TIME_FIELD)} DESC, {quote_identifier(ID_FIELD)} DESC LIMIT 1"
        )
        row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"实时表 {table_name} 中没有数据")
    return row


def fetch_history_rows(conn, table_name: str, ring_no: int) -> List[Dict[str, Any]]:
    table = quote_identifier(table_name)
    fields = ", ".join(quote_identifier(name) for name in HISTORY_FIELDS)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {fields} FROM {table} "
            f"WHERE {quote_identifier(RING_FIELD)}=%s "
            f"ORDER BY {quote_identifier(TIME_FIELD)} ASC, {quote_identifier(ID_FIELD)} ASC",
            (ring_no,),
        )
        return list(cursor.fetchall())


def fetch_target(conn, table_name: str, ring_no: int) -> Optional[Dict[str, Any]]:
    table = quote_identifier(table_name)
    fields = ", ".join(quote_identifier(name) for name in TARGET_FIELDS)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {fields} FROM {table} "
            "WHERE `ring_start` <= %s AND `ring_end` >= %s "
            "ORDER BY `update_date` DESC, `id` DESC LIMIT 1",
            (ring_no, ring_no),
        )
        return cursor.fetchone()


def alarm_select_fields() -> str:
    fields: List[str] = []
    for name in ALARM_FIELDS:
        if name == "alarm_desc":
            continue
        if name == "alarm_subtype":
            fields.append("HEX(`alarm_subtype`) AS `alarm_subtype_hex`")
        else:
            fields.append(quote_identifier(name))
    fields.append("HEX(`alarm_desc`) AS `alarm_desc_hex`")
    return ", ".join(fields)


def convert_alarm_row(row: Dict[str, Any]) -> Dict[str, Any]:
    converted = dict(row)
    converted["alarm_desc"] = normalize_alarm_text(decode_hex_text(row.get("alarm_desc_hex")))
    converted["alarm_subtype"] = decode_hex_text(row.get("alarm_subtype_hex"))
    return converted


def fetch_recent_alarm_samples(conn, table_name: str, limit: int = 200) -> List[Dict[str, Any]]:
    table = quote_identifier(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {alarm_select_fields()} FROM {table} "
            "WHERE `alarm_status`=0 AND `alarm_desc` IS NOT NULL AND TRIM(`alarm_desc`)<>'' "
            "ORDER BY COALESCE(`alarm_start`,`create_time`) DESC, `id` DESC LIMIT %s",
            (limit,),
        )
        return [convert_alarm_row(row) for row in cursor.fetchall()]


def fetch_alarm_by_id(conn, table_name: str, alarm_id: int) -> Optional[Dict[str, Any]]:
    table = quote_identifier(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {alarm_select_fields()} FROM {table} WHERE `id`=%s LIMIT 1",
            (alarm_id,),
        )
        row = cursor.fetchone()
    return convert_alarm_row(row) if row else None


def fetch_table_counts(conn, table_names: Dict[str, str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with conn.cursor() as cursor:
        for logical_name, table_name in table_names.items():
            cursor.execute(f"SELECT COUNT(*) AS `count` FROM {quote_identifier(table_name)}")
            row = cursor.fetchone() or {}
            counts[logical_name] = int(row.get("count") or 0)
    return counts


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_summary_ring(realtime_row: Dict[str, Any]) -> Tuple[Optional[int], str]:
    current_ring = to_int(realtime_row.get(RING_FIELD))
    if current_ring is None:
        return None, "invalid_current_ring"
    return current_ring, "current_realtime_ring"


def evaluate_completion(
    realtime_row: Dict[str, Any],
    summary_ring: Optional[int],
    history_rows: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    current_ring = to_int(realtime_row.get(RING_FIELD))
    atmd02 = to_int(realtime_row.get("ATMD02"))
    stroke = to_float(realtime_row.get("ATBA02"))
    completion_status = atmd02 == 1
    stroke_gate = stroke is not None and stroke >= args.min_stroke_check
    history_available = bool(history_rows)
    passed = (
        summary_ring is not None
        and completion_status
        and stroke_gate
        and history_available
    )
    reasons = {
        "repeat_allowed": True,
        "completion_status_ATMD02": completion_status,
        "stroke_gate": stroke_gate,
        "history_available": history_available,
        "current_ring": current_ring,
        "summary_ring": summary_ring,
        "ATMD02": atmd02,
        "ATBA02": stroke,
        "min_stroke_check": args.min_stroke_check,
        "status": "passed" if passed else "not_triggered",
    }
    return reasons


def target_interval_evaluation(
    metric: Dict[str, Any],
    target_start: Any,
    target_end: Any,
    label: str,
    args: argparse.Namespace,
    compare_enabled: bool = True,
) -> Dict[str, Any]:
    actual = to_float(metric.get("end_mean")) or to_float(metric.get("mean"))
    start = to_float(target_start)
    end = to_float(target_end)
    result: Dict[str, Any] = {
        "label": label,
        "actual_end": actual,
        "target_start": start,
        "target_end": end,
        "planning_range": (
            [min(start, end), max(start, end)]
            if start is not None and end is not None
            else None
        ),
    }
    if actual is None or start is None or end is None:
        result.update({"status": "unknown", "conclusion": "实际值或规划区间缺失，暂无法评价"})
        return result
    if not compare_enabled:
        result.update({"status": "unit_pending", "conclusion": ""})
        return result
    lower, upper = min(start, end), max(start, end)
    if actual < lower:
        status = "below_planning_range"
        conclusion = f"{label}低于规划区间下界"
    elif actual > upper:
        status = "above_planning_range"
        conclusion = f"{label}高于规划区间上界"
    else:
        deviation = relative_deviation(actual, end)
        if deviation is not None and deviation <= args.relative_tolerance:
            status = "near_end_target"
            conclusion = f"{label}处于规划区间内并接近结束目标"
        elif actual < end:
            status = "within_range_below_end"
            conclusion = f"{label}处于规划区间内但低于结束目标"
        else:
            status = "within_range_above_end"
            conclusion = f"{label}处于规划区间内但高于结束目标"
    result.update(
        {
            "status": status,
            "conclusion": conclusion,
            "relative_deviation_to_end": round_or_none(relative_deviation(actual, end), 4),
        }
    )
    return result


def posture_evaluation(
    summary: Dict[str, Any],
    target: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if not target:
        return {"status": "unknown", "conclusion": "未匹配控制目标，姿态暂无法评价"}

    mapping = {"1": "head", "0": "tail"}
    selected_h = mapping.get(str(to_int(target.get("horizontal_pos"))), args.fallback_endpoint)
    selected_v = mapping.get(str(to_int(target.get("vertical_pos"))), args.fallback_endpoint)

    for axis, selected, target_value in (
        ("horizontal", selected_h, to_float(target.get("horizontal_pos_dist"))),
        ("vertical", selected_v, to_float(target.get("vertical_pos_dist"))),
    ):
        metric = summary["posture"].get(f"{axis}_{selected}", {})
        start = to_float(metric.get("start_mean"))
        end = to_float(metric.get("end_mean"))
        if start is None or end is None or target_value is None:
            result[axis] = {
                "selected_endpoint": selected,
                "target": target_value,
                "status": "unknown",
                "conclusion": "姿态数据或目标缺失，暂无法评价",
            }
            continue
        d_start = abs(start - target_value)
        d_end = abs(end - target_value)
        improvement = d_start - d_end
        if improvement > args.posture_epsilon:
            status = "toward_target"
            conclusion = "向目标靠近"
        elif improvement < -args.posture_epsilon:
            status = "away_from_target"
            conclusion = "偏离目标，需持续观察"
        else:
            status = "stable"
            conclusion = "基本保持稳定"
        result[axis] = {
            "selected_endpoint": selected,
            "target": target_value,
            "start": start,
            "end": end,
            "distance_start": round(d_start, 3),
            "distance_end": round(d_end, 3),
            "distance_improvement": round(improvement, 3),
            "status": status,
            "conclusion": conclusion,
        }
    statuses = {item.get("status") for item in result.values()}
    if "away_from_target" in statuses:
        overall = "存在姿态偏离项"
    elif "toward_target" in statuses:
        overall = "姿态向目标靠近"
    elif statuses == {"stable"}:
        overall = "姿态基本稳定"
    else:
        overall = "姿态部分可评价"
    return {
        "status": "attention" if "away_from_target" in statuses else "available",
        "conclusion": overall,
        "selected_endpoint_mapping": "目标值1按前端/切口、0按后端/盾尾解释；正式语义仍需平台确认",
        "axes": result,
    }


def evaluate_control(
    summary: Dict[str, Any],
    target: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    if not target:
        return {
            "target_status": "unmatched",
            "target_id": None,
            "speed": target_interval_evaluation(summary["speed"], None, None, "平均推进速度", args),
            "total_thrust": target_interval_evaluation(summary["total_thrust"], None, None, "总推力", args),
            "cutter_torque": target_interval_evaluation(summary["cutter_torque"], None, None, "刀盘扭矩", args),
            "posture": posture_evaluation(summary, None, args),
        }
    return {
        "target_status": "matched",
        "target_id": target.get("id"),
        "speed": target_interval_evaluation(
            summary["speed"],
            target.get("speed_start"),
            target.get("speed_end"),
            "平均推进速度",
            args,
            compare_enabled=args.compare_speed_target,
        ),
        "total_thrust": target_interval_evaluation(
            summary["total_thrust"],
            target.get("thrust_start"),
            target.get("thrust_end"),
            "总推力",
            args,
        ),
        "cutter_torque": target_interval_evaluation(
            summary["cutter_torque"],
            target.get("torque_start"),
            target.get("torque_end"),
            "刀盘扭矩",
            args,
        ),
        "posture": posture_evaluation(summary, target, args),
    }


def active_realtime_alarm_fields(row: Dict[str, Any]) -> List[str]:
    active: List[str] = []
    for field_name, value in row.items():
        if re.fullmatch(r"T\d{5}", str(field_name)) and is_alarm_active(value):
            active.append(str(field_name))
    return sorted(active)


def build_trigger_events(
    realtime_row: Dict[str, Any],
    summary: Optional[Dict[str, Any]],
    control: Dict[str, Any],
    completion: Dict[str, Any],
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for field_name in active_realtime_alarm_fields(realtime_row):
        events.append(
            {
                "trigger_type": "realtime_alarm_bit",
                "risk_object": ALARM_LABELS.get(field_name, field_name),
                "risk_level": "warning" if field_name == "T00054" else "attention",
                "alarm_field": field_name,
                "reason": f"实时报警位 {field_name} 处于有效状态",
            }
        )

    for key, object_name in (
        ("total_thrust", "总推力"),
        ("cutter_torque", "刀盘扭矩"),
    ):
        status = control.get(key, {}).get("status")
        if status in {"below_planning_range", "above_planning_range"}:
            events.append(
                {
                    "trigger_type": "target_deviation",
                    "risk_object": object_name,
                    "risk_level": "attention",
                    "reason": control[key].get("conclusion"),
                }
            )

    posture = control.get("posture", {})
    if posture.get("status") == "attention":
        events.append(
            {
                "trigger_type": "target_deviation",
                "risk_object": "姿态控制",
                "risk_level": "attention",
                "reason": posture.get("conclusion"),
            }
        )

    if summary is not None and summary.get("missing_core_fields"):
        events.append(
            {
                "trigger_type": "data_quality",
                "risk_object": "关键掘进数据",
                "risk_level": "attention",
                "reason": "关键字段有效值不足",
                "missing_fields": summary["missing_core_fields"],
            }
        )

    if completion.get("current_ring") is None:
        events.append(
            {
                "trigger_type": "data_quality",
                "risk_object": "环号",
                "risk_level": "attention",
                "reason": "实时快照缺少 AVBA10",
            }
        )

    if args.alarm_id is not None:
        events.append(
            {
                "trigger_type": "specified_alarm_sample",
                "risk_object": "指定历史报警样本",
                "risk_level": "attention",
                "reason": "历史报警样本文本接入",
                "alarm_id": args.alarm_id,
            }
        )
    return events


def select_alarm_sample(
    samples: Sequence[Dict[str, Any]],
    event: Dict[str, Any],
    realtime_time: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    if not samples:
        return None
    if event.get("trigger_type") == "specified_alarm_sample":
        requested_id = event.get("alarm_id")
        for sample in samples:
            if to_int(sample.get("id")) == requested_id:
                return sample

    risk_object = str(event.get("risk_object", ""))
    keywords = ALARM_KEYWORDS.get(risk_object, ())
    best: Optional[Tuple[int, datetime, Dict[str, Any]]] = None
    for sample in samples:
        desc = str(sample.get("alarm_desc") or "")
        score = 0
        if event.get("alarm_field") and sample.get("alarm_field") == event["alarm_field"]:
            score += 10
        score += sum(2 for keyword in keywords if keyword in desc)
        sample_time = parse_datetime(sample.get("alarm_start") or sample.get("create_time")) or datetime.min
        candidate = (score, sample_time, sample)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None or best[0] <= 0:
        return None
    return best[2]


def process_safety_warning(
    events: Sequence[Dict[str, Any]],
    alarm_samples: Sequence[Dict[str, Any]],
    realtime_row: Dict[str, Any],
) -> Dict[str, Any]:
    if not events:
        return {
            "status": "not_triggered",
            "trigger_count": 0,
            "risk_level": "normal",
            "message": "当前未满足安全预警触发条件。",
            "detail": "当前实时状态未达到已配置的安全预警触发条件。",
            "events": [],
            "alarm_sample": None,
            "repeat_policy": "仅在本次触发条件成立时生成",
        }

    rank = {"normal": 0, "attention": 1, "warning": 2}
    primary = max(events, key=lambda item: rank.get(item.get("risk_level", "attention"), 1))
    sample_event = next(
        (event for event in events if event.get("trigger_type") == "specified_alarm_sample"),
        primary,
    )
    sample = select_alarm_sample(
        alarm_samples,
        sample_event,
        parse_datetime(realtime_row.get(TIME_FIELD)),
    )
    risk_level = primary.get("risk_level", "attention")
    risk_level_text = {"normal": "正常", "attention": "关注", "warning": "警示"}.get(
        risk_level,
        "关注",
    )
    event_text = "；".join(str(event.get("reason")) for event in events[:3])
    sample_text = str(sample.get("alarm_desc") or "").strip() if sample else ""
    if not sample_text:
        sample_text = "请按当前触发依据进行现场核对"
    status = "triggered"
    sample_reference = (
        "历史报警文本参考为"
        if sample_event.get("trigger_type") == "specified_alarm_sample"
        else "历史同类信息参考为"
    )
    brief = (
        f"安全预警：{primary.get('risk_object')}出现关注状态，{sample_text}。"
        f"请现场人员核对{primary.get('risk_object')}及相关工况。"
        "该条播报不代表最终安全裁决，请现场人员介入判断。"
    )
    detail = (
        "【安全预警】\n"
        f"{primary.get('risk_object')}当前为{risk_level_text}级预警。\n"
        "【触发依据】\n"
        f"{event_text}。{sample_reference}：{sample_text}。\n"
        "【现场判断】\n"
        f"请现场人员核对{primary.get('risk_object')}、实时数据和设备状态，"
        "必要时由中枢或判断模块进一步确认。\n"
        "该条播报不代表最终安全裁决，请现场人员介入判断。"
    )

    return {
        "status": status,
        "trigger_count": len(events),
        "risk_level": risk_level,
        "message": brief,
        "detail": detail,
        "events": list(events),
        "alarm_sample": {
            "id": sample.get("id"),
            "alarm_desc": sample.get("alarm_desc"),
            "alarm_type": sample.get("alarm_type"),
            "alarm_level": sample.get("alarm_level"),
            "alarm_field": sample.get("alarm_field"),
            "alarm_status": sample.get("alarm_status"),
            "alarm_md5": sample.get("alarm_md5"),
        }
        if sample
        else None,
        "repeat_policy": "每次运行只要触发条件成立，均重新生成安全预警",
    }


def ring_summary_messages(
    summary: Dict[str, Any],
    control: Dict[str, Any],
    completion: Dict[str, Any],
) -> Dict[str, str]:
    ring_no = summary["ring_no"]
    speed = summary["speed"]
    thrust = summary["total_thrust"]
    torque = summary["cutter_torque"]
    posture = control.get("posture", {})

    speed_text = (
        f"{fmt_num(speed.get('mean'))}"
        if speed.get("mean") is not None
        else "暂无法评价"
    )
    thrust_text = (
        f"{fmt_num(thrust.get('mean'))}"
        if thrust.get("mean") is not None
        else "暂无法评价"
    )
    torque_text = (
        f"{fmt_num(torque.get('mean'))}"
        if torque.get("mean") is not None
        else "暂无法评价"
    )
    target_texts = [
        control.get("total_thrust", {}).get("conclusion"),
        control.get("cutter_torque", {}).get("conclusion"),
        control.get("speed", {}).get("conclusion"),
    ]
    target_text = "；".join(text for text in target_texts if text)
    brief = (
        f"第 {ring_no} 环推进完成。平均推进速度 {speed_text}，"
        f"总推力环均值 {thrust_text}，刀盘扭矩环均值 {torque_text}。"
        f"{posture.get('conclusion', '姿态暂无法评价')}。{target_text}。"
    )
    detail = (
        "【环完成】\n"
        f"第 {ring_no} 环推进完成，里程由 {fmt_value_or_pending(summary.get('mileage_start'))}"
        f" 变化至 {fmt_value_or_pending(summary.get('mileage_end'))}，"
        f"推进净行程最大值为 {fmt_value_or_pending(summary.get('stroke_max'))}，"
        f"有效历史记录 {summary.get('record_count')} 条。\n"
        "【工况与控制效果】\n"
        f"平均推进速度为 {speed_text}；总推力环均值为 "
        f"{fmt_num(thrust.get('mean'))}；刀盘扭矩环均值为 "
        f"{fmt_num(torque.get('mean'))}。\n"
        f"目标评价：{target_text or '暂无法评价'}。\n"
        "【姿态评价】\n"
        f"{posture.get('conclusion', '姿态暂无法评价')}。"
        f"水平：{posture.get('axes', {}).get('horizontal', {}).get('conclusion', '暂无法评价')}；"
        f"垂直：{posture.get('axes', {}).get('vertical', {}).get('conclusion', '暂无法评价')}。\n"
        "【数据边界】\n"
        f"本环历史数据有效性：{summary.get('record_count')} 条记录，"
        f"速度有效样本 {speed.get('valid_count')}/{speed.get('total_count')}，"
        f"推力有效样本 {thrust.get('valid_count')}/{thrust.get('total_count')}，"
        f"扭矩有效样本 {torque.get('valid_count')}/{torque.get('total_count')}。"
    )
    return {"brief": brief, "detail": detail}


def build_output(
    realtime_row: Dict[str, Any],
    summary: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
    control: Dict[str, Any],
    completion: Dict[str, Any],
    safety: Dict[str, Any],
    table_counts: Dict[str, int],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    if summary is not None and completion.get("status") == "passed":
        ring_messages = ring_summary_messages(summary, control, completion)
        ring_status = "generated"
    else:
        ring_messages = {
            "brief": "本次未满足环总结自动触发条件，未生成新的环总结播报。",
            "detail": "当前实时环未同时满足完成状态、推进净行程和历史数据可用条件。",
        }
        ring_status = completion.get("status", "not_triggered")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": {
            "host": args.host,
            "port": args.port,
            "database": args.database,
            "read_only": True,
            "tables": {
                "realtime": args.realtime_table,
                "history": args.history_table,
                "target": args.target_table,
                "alarm": args.alarm_table,
            },
            "row_counts": table_counts,
        },
        "runtime": {
            "window_size": args.window_size,
            "min_stroke_check": args.min_stroke_check,
            "relative_tolerance": args.relative_tolerance,
            "compare_speed_target": args.compare_speed_target,
            "posture_epsilon": args.posture_epsilon,
        },
        "trigger": completion,
        "realtime_snapshot": {
            "ring_no": to_int(realtime_row.get(RING_FIELD)),
            "localtime1": iso_datetime(realtime_row.get(TIME_FIELD)),
            "ATMD02": to_int(realtime_row.get("ATMD02")),
            "ATBA02": to_float(realtime_row.get("ATBA02")),
            "active_alarm_fields": active_realtime_alarm_fields(realtime_row),
        },
        "ring_summary": {
            "status": ring_status,
            "ring_no": summary.get("ring_no") if summary else None,
            "summary": summary,
            "target": target,
            "control_evaluation": control,
            "messages": ring_messages,
        },
        "safety_warning": safety,
    }


def write_outputs(output: Dict[str, Any], output_dir: Path) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "环播报输出.json"
    md_path = output_dir / "环播报输出.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ring = output["ring_summary"]
    safety = output["safety_warning"]
    markdown = (
        "# 环播报输出\n\n"
        "## 环总结播报\n\n"
        "### 简约版\n"
        f"{ring['messages']['brief']}\n\n"
        "### 详细版\n"
        f"{ring['messages']['detail']}\n\n"
        "## 安全预警播报\n\n"
        "### 简约版\n"
        f"{safety['message']}\n\n"
        "### 详细版\n"
        f"{safety['detail']}\n"
    )
    md_path.write_text(markdown, encoding="utf-8")
    return md_path, json_path


def parse_args() -> argparse.Namespace:
    program_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="盾构播报机器人第五周 MySQL 只读程序")
    parser.add_argument("--alarm-id", type=int, default=None, help="指定alarm_record.id验证历史预警文本接入")
    parser.add_argument("--host", default="127.0.0.1", help="MySQL主机")
    parser.add_argument("--port", type=int, default=3306, help="MySQL端口")
    parser.add_argument("--user", default="root", help="MySQL用户")
    parser.add_argument("--password", default="", help="MySQL密码")
    parser.add_argument("--database", default="rcbi", help="数据库名")
    parser.add_argument("--realtime-table", default="mb_data_auto", help="实时数据表")
    parser.add_argument("--history-table", default="mb_data_his_auto", help="历史数据表")
    parser.add_argument("--target-table", default="config_target_manage", help="控制目标表")
    parser.add_argument("--alarm-table", default="alarm_record", help="历史报警表")
    parser.add_argument("--output-dir", default=str(program_dir / "outputs"), help="输出目录")
    parser.add_argument("--state-file", default=str(program_dir / "runtime_state.json"), help="本地运行状态文件")
    parser.add_argument("--window-size", type=int, default=10, help="内部起始/末段统计窗口样本数")
    parser.add_argument("--min-stroke-check", type=float, default=1000.0, help="推进净行程测试校验值")
    parser.add_argument("--relative-tolerance", type=float, default=0.10, help="目标评价初始相对容差")
    parser.add_argument("--posture-epsilon", type=float, default=1.0, help="姿态距离变化判断容差")
    parser.add_argument(
        "--compare-speed-target",
        action="store_true",
        help="确认速度单位兼容后才开启速度目标比较，默认关闭",
    )
    parser.add_argument(
        "--fallback-endpoint",
        choices=("head", "tail"),
        default="head",
        help="姿态选择编码不明确时的回退端",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> Dict[str, Any]:
    state_path = Path(args.state_file)
    state = load_json(state_path, {})
    # 安全预警不使用历史降频状态；清理旧版本遗留的本地缓存字段。
    state.pop("alarm_state", None)

    table_names = {
        "mb_data_auto": args.realtime_table,
        "mb_data_his_auto": args.history_table,
        "config_target_manage": args.target_table,
        "alarm_record": args.alarm_table,
    }

    with connect_mysql(args) as conn:
        realtime_row = fetch_latest_realtime(conn, args.realtime_table)
        summary_ring, _ = choose_summary_ring(realtime_row)
        current_ring = to_int(realtime_row.get(RING_FIELD))

        history_rows: List[Dict[str, Any]] = []
        if summary_ring is not None:
            history_rows = fetch_history_rows(conn, args.history_table, summary_ring)

        summary = (
            summarize_ring(history_rows, summary_ring, args.window_size)
            if history_rows and summary_ring is not None
            else None
        )
        target_ring = summary_ring if summary is not None else current_ring
        target = fetch_target(conn, args.target_table, target_ring) if target_ring is not None else None
        control = evaluate_control(summary, target, args) if summary is not None else {
            "target_status": "not_evaluated",
            "speed": {},
            "total_thrust": {},
            "cutter_torque": {},
            "posture": {},
        }

        completion = evaluate_completion(realtime_row, summary_ring, history_rows, args)
        evaluation_ready = completion.get("status") == "passed"
        events = build_trigger_events(
            realtime_row,
            summary if evaluation_ready else None,
            control if evaluation_ready else {},
            completion,
            args,
        )
        alarm_samples = fetch_recent_alarm_samples(conn, args.alarm_table)
        if args.alarm_id is not None:
            selected_alarm = fetch_alarm_by_id(conn, args.alarm_table, args.alarm_id)
            if selected_alarm is not None:
                alarm_samples = [selected_alarm] + [
                    sample for sample in alarm_samples if sample.get("id") != selected_alarm.get("id")
                ]
        table_counts = fetch_table_counts(conn, table_names)

    safety = process_safety_warning(events, alarm_samples, realtime_row)
    output = build_output(
        realtime_row,
        summary if completion.get("status") == "passed" else None,
        target,
        control,
        completion,
        safety,
        table_counts,
        args,
    )

    state["last_seen_ring"] = current_ring
    state["last_snapshot_time"] = iso_datetime(realtime_row.get(TIME_FIELD))
    state["last_summary_ring"] = summary_ring
    save_json(state_path, state)
    return output


def main() -> int:
    args = parse_args()
    try:
        output = run(args)
        md_path, json_path = write_outputs(output, Path(args.output_dir))
    except Exception as exc:
        print(f"程序运行失败：{exc}")
        return 1

    print("程序运行完成。")
    print(f"环总结状态：{output['ring_summary']['status']}")
    print(f"安全预警状态：{output['safety_warning']['status']}")
    print(f"Markdown：{md_path.resolve()}")
    print(f"JSON：{json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
