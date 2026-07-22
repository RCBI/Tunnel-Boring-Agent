#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盾构播报机器人 demo

功能：
1. 解析项目提供的 19 号线 22 标 SQL 数据文件；
2. 按读取环号 AVBA10 聚合 mb_data_his_auto 历史数据；
3. 基于真实字段生成“环总结 + 安全警示”三版播报；
4. 导出 Markdown 和 JSON 结果。

"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


DEFAULT_RING_NO = 1028
DEFAULT_SQL_FILE = "mb_data_his_auto历史掘进数据.sql"

RING_FIELD = "AVBA10"
TIME_FIELD = "localtime1"

P0_FIELDS = [
    "id",
    "localtime1",
    "AVBA10",
    "AVBA02",
    "ATBA02",
    "ATBS01",
    "AZVS01",
    "ATBA03",
    "ACBA03",
    "ABBA02",
    "ABBA04",
    "AVSA01",
    "AVSA02",
    "AVSA11",
    "AVSA12",
]

ALARM_FIELDS = [
    "T00029",
    "T00030",
    "T00031",
    "T00037",
    "T00038",
    "T00054",
    "T00067",
    "T00110",
    "T00142",
]

NEEDED_FIELDS = P0_FIELDS + ALARM_FIELDS


@dataclass
class NumericAgg:
    total: float = 0.0
    count: int = 0
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def add(self, value: Any) -> None:
        number = to_float_or_none(value)
        if number is None:
            return
        self.total += number
        self.count += 1
        self.min_value = number if self.min_value is None else min(self.min_value, number)
        self.max_value = number if self.max_value is None else max(self.max_value, number)

    @property
    def avg(self) -> Optional[float]:
        if self.count == 0:
            return None
        return self.total / self.count


@dataclass
class RingAgg:
    ring_no: int
    record_count: int = 0
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    fields: Dict[str, NumericAgg] = field(default_factory=dict)
    alarm_counts: Dict[str, int] = field(default_factory=lambda: {name: 0 for name in ALARM_FIELDS})

    def metric(self, field_name: str) -> NumericAgg:
        if field_name not in self.fields:
            self.fields[field_name] = NumericAgg()
        return self.fields[field_name]

    def add_row(self, row: Dict[str, Any]) -> None:
        self.record_count += 1
        sample_time = row.get(TIME_FIELD)
        if sample_time:
            sample_text = str(sample_time)
            self.start_time = sample_text if self.start_time is None else min(self.start_time, sample_text)
            self.end_time = sample_text if self.end_time is None else max(self.end_time, sample_text)

        for field_name in P0_FIELDS:
            if field_name in ("id", TIME_FIELD, RING_FIELD):
                continue
            self.metric(field_name).add(row.get(field_name))

        for alarm_field in ALARM_FIELDS:
            value = row.get(alarm_field)
            if value not in (None, "", "NULL"):
                self.alarm_counts[alarm_field] += 1


def to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def round_or_none(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def fmt_num(value: Any, digits: int = 1, keep_decimal: bool = False) -> str:
    number = to_float_or_none(value)
    if number is None:
        return "无数据"
    if keep_decimal:
        return f"{number:.{digits}f}"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def split_sql_tuple(tuple_text: str) -> List[Any]:
    """Parse one SQL tuple such as (1, 'time', NULL)."""
    values: List[Any] = []
    current: List[str] = []
    in_quote = False
    escape = False

    inner = tuple_text[1:-1]
    for char in inner:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\" and in_quote:
            escape = True
            continue
        if char == "'":
            in_quote = not in_quote
            continue
        if char == "," and not in_quote:
            values.append(convert_sql_value("".join(current).strip()))
            current = []
            continue
        current.append(char)

    values.append(convert_sql_value("".join(current).strip()))
    return values


def convert_sql_value(text: str) -> Any:
    if text.upper() == "NULL":
        return None
    if text == "":
        return ""
    return text


def iter_tuple_text(values_text: str) -> Iterator[str]:
    """Yield top-level tuple strings from the VALUES part of an INSERT."""
    in_quote = False
    escape = False
    depth = 0
    start: Optional[int] = None

    for index, char in enumerate(values_text):
        if escape:
            escape = False
            continue
        if char == "\\" and in_quote:
            escape = True
            continue
        if char == "'":
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and start is not None:
                yield values_text[start : index + 1]
                start = None


def iter_insert_statements(sql_path: Path) -> Iterator[str]:
    """Yield INSERT statements for mb_data_his_auto from a SQL dump."""
    buffer: List[str] = []
    capturing = False

    with sql_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not capturing and line.startswith("INSERT INTO `mb_data_his_auto`"):
                capturing = True
                buffer = [line]
                if line.rstrip().endswith(";"):
                    yield "".join(buffer)
                    capturing = False
                continue
            if capturing:
                buffer.append(line)
                if line.rstrip().endswith(";"):
                    yield "".join(buffer)
                    capturing = False


def iter_selected_rows(sql_path: Path, needed_fields: Iterable[str]) -> Iterator[Dict[str, Any]]:
    """Yield selected columns from mb_data_his_auto INSERT statements."""
    needed = list(needed_fields)
    pattern = re.compile(r"INSERT INTO `mb_data_his_auto` \((.*?)\) VALUES\s*(.*);?\s*$", re.S)
    field_indexes: Optional[Dict[str, int]] = None

    for statement in iter_insert_statements(sql_path):
        match = pattern.match(statement)
        if not match:
            continue
        columns_text, values_text = match.groups()
        columns = re.findall(r"`([^`]+)`", columns_text)
        if field_indexes is None:
            missing = [name for name in needed if name not in columns]
            if missing:
                raise ValueError("SQL 文件缺少必要字段：" + "、".join(missing))
            field_indexes = {name: columns.index(name) for name in needed}

        for tuple_text in iter_tuple_text(values_text):
            values = split_sql_tuple(tuple_text)
            yield {name: values[index] if index < len(values) else None for name, index in field_indexes.items()}


def load_ring_aggs(sql_path: Path) -> Dict[int, RingAgg]:
    if not sql_path.exists():
        raise FileNotFoundError(f"未找到 SQL 数据文件：{sql_path}")

    rings: Dict[int, RingAgg] = {}
    for row in iter_selected_rows(sql_path, NEEDED_FIELDS):
        ring_value = to_float_or_none(row.get(RING_FIELD))
        if ring_value is None:
            continue
        ring_no = int(round(ring_value))
        if ring_no not in rings:
            rings[ring_no] = RingAgg(ring_no=ring_no)
        rings[ring_no].add_row(row)

    if not rings:
        raise ValueError("SQL 文件中未解析到可用环数据。")
    return rings


def duration_minutes(start_time: Optional[str], end_time: Optional[str]) -> Optional[float]:
    if not start_time or not end_time:
        return None
    start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
    return round((end - start).total_seconds() / 60, 1)


def agg_avg(ring: RingAgg, field_name: str, digits: int = 1) -> Optional[float]:
    return round_or_none(ring.metric(field_name).avg, digits)


def agg_min(ring: RingAgg, field_name: str, digits: int = 3) -> Optional[float]:
    return round_or_none(ring.metric(field_name).min_value, digits)


def agg_max(ring: RingAgg, field_name: str, digits: int = 3) -> Optional[float]:
    return round_or_none(ring.metric(field_name).max_value, digits)


def finalize_ring_summary(ring: RingAgg) -> Dict[str, Any]:
    return {
        "ring_no": ring.ring_no,
        "record_count": ring.record_count,
        "start_time": ring.start_time,
        "end_time": ring.end_time,
        "duration_min": duration_minutes(ring.start_time, ring.end_time),
        "mileage_start": agg_min(ring, "AVBA02", 3),
        "mileage_end": agg_max(ring, "AVBA02", 3),
        "stroke_min": int(agg_min(ring, "ATBA02", 0) or 0),
        "stroke_max": int(agg_max(ring, "ATBA02", 0) or 0),
        "avg_speed_set": agg_avg(ring, "ATBS01", 1),
        "avg_smart_pump_set": agg_avg(ring, "AZVS01", 1),
        "avg_thrust": agg_avg(ring, "ATBA03", 1),
        "max_thrust": int(agg_max(ring, "ATBA03", 0) or 0),
        "avg_torque": agg_avg(ring, "ACBA03", 1),
        "max_torque": int(agg_max(ring, "ACBA03", 0) or 0),
        "avg_earth_p_r": agg_avg(ring, "ABBA02", 2),
        "avg_earth_p_l": agg_avg(ring, "ABBA04", 2),
        "h_front_min": agg_min(ring, "AVSA01", 0),
        "h_front_max": agg_max(ring, "AVSA01", 0),
        "v_front_min": agg_min(ring, "AVSA02", 0),
        "v_front_max": agg_max(ring, "AVSA02", 0),
        "h_tail_min": agg_min(ring, "AVSA11", 0),
        "h_tail_max": agg_max(ring, "AVSA11", 0),
        "v_tail_min": agg_min(ring, "AVSA12", 0),
        "v_tail_max": agg_max(ring, "AVSA12", 0),
        "alarm_counts": dict(ring.alarm_counts),
    }


def mean(values: List[float]) -> float:
    return sum(values) / len(values)


def stddev_pop(values: List[float]) -> float:
    base = mean(values)
    return math.sqrt(sum((item - base) ** 2 for item in values) / len(values))


def threshold_pair(values: List[float], digits: int) -> Dict[str, float]:
    avg = round(mean(values), digits)
    std = round(stddev_pop(values), digits)
    return {
        "mean": avg,
        "std": std,
        "attention": round(avg + 2 * std, digits),
        "serious": round(avg + 3 * std, digits),
    }


def build_thresholds(summaries: Dict[int, Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    rows = list(summaries.values())
    return {
        "max_torque": threshold_pair([row["max_torque"] for row in rows], 1),
        "avg_torque": threshold_pair([row["avg_torque"] for row in rows], 1),
        "max_thrust": threshold_pair([row["max_thrust"] for row in rows], 1),
        "earth_pressure_right": threshold_pair([row["avg_earth_p_r"] for row in rows], 2),
        "earth_pressure_left": threshold_pair([row["avg_earth_p_l"] for row in rows], 2),
        "horizontal_front_offset_abs": threshold_pair(
            [max(abs(row["h_front_min"]), abs(row["h_front_max"])) for row in rows], 1
        ),
        "vertical_front_offset_abs": threshold_pair(
            [max(abs(row["v_front_min"]), abs(row["v_front_max"])) for row in rows], 1
        ),
        "horizontal_tail_offset_abs": threshold_pair(
            [max(abs(row["h_tail_min"]), abs(row["h_tail_max"])) for row in rows], 1
        ),
        "vertical_tail_offset_abs": threshold_pair(
            [max(abs(row["v_tail_min"]), abs(row["v_tail_max"])) for row in rows], 1
        ),
    }


def evaluate_risk(summary: Dict[str, Any], thresholds: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    alarm_counts = summary["alarm_counts"]
    triggered_alarms = [name for name, count in alarm_counts.items() if count > 0]
    reasons: List[str] = []
    serious = False

    if triggered_alarms:
        reasons.append("检测到报警位字段非空：" + "、".join(triggered_alarms))
        serious = "T00054" in triggered_alarms

    checks = [
        ("最大刀盘扭矩", "max_torque", summary["max_torque"], 1),
        ("平均刀盘扭矩", "avg_torque", summary["avg_torque"], 1),
        ("最大总推力", "max_thrust", summary["max_thrust"], 1),
        ("右侧平均土压", "earth_pressure_right", summary["avg_earth_p_r"], 2),
        ("左侧平均土压", "earth_pressure_left", summary["avg_earth_p_l"], 2),
        ("前端水平偏差最大绝对值", "horizontal_front_offset_abs", max(abs(summary["h_front_min"]), abs(summary["h_front_max"])), 1),
        ("前端垂直偏差最大绝对值", "vertical_front_offset_abs", max(abs(summary["v_front_min"]), abs(summary["v_front_max"])), 1),
        ("后端水平偏差最大绝对值", "horizontal_tail_offset_abs", max(abs(summary["h_tail_min"]), abs(summary["h_tail_max"])), 1),
        ("后端垂直偏差最大绝对值", "vertical_tail_offset_abs", max(abs(summary["v_tail_min"]), abs(summary["v_tail_max"])), 1),
    ]

    for label, threshold_key, value, digits in checks:
        threshold = thresholds[threshold_key]
        if value >= threshold["serious"]:
            serious = True
            reasons.append(f"{label} {fmt_num(value, digits)} 高于严重阈值 {fmt_num(threshold['serious'], digits, True)}")
        elif value >= threshold["attention"]:
            reasons.append(f"{label} {fmt_num(value, digits)} 高于关注阈值 {fmt_num(threshold['attention'], digits, True)}")

    if serious:
        level = "严重"
    elif triggered_alarms:
        level = "警示"
    elif reasons:
        level = "关注"
    else:
        level = "正常"
        reasons.append("当前报警位字段未触发，关键连续参数未超过演示关注阈值")

    return {
        "risk_level": level,
        "risk_reason": "，".join(reasons),
        "triggered_alarms": triggered_alarms,
        "threshold_source": "本批 1028-1047 共 20 环历史数据统计演示规则，正式阈值仍需确认",
    }


def fmt_range(min_value: Any, max_value: Any) -> str:
    return f"{fmt_num(min_value, 0)} 至 {fmt_num(max_value, 0)}"


def build_messages(summary: Dict[str, Any], risk: Dict[str, Any]) -> Dict[str, str]:
    ring_no = summary["ring_no"]
    confirm_question = "请现场核对姿态偏差、管片拼装和司机感知是否存在异常。"
    if risk["risk_level"] == "正常":
        confirm_question = "请现场确认是否有异常情况需要记录。"

    brief = (
        f"第 {ring_no} 环已完成，本环持续 {fmt_num(summary['duration_min'])} 分钟，"
        f"推进速度设定均值 {fmt_num(summary['avg_speed_set'])}，平均总推力 {fmt_num(summary['avg_thrust'])}，"
        f"平均刀盘扭矩 {fmt_num(summary['avg_torque'])}。\n"
        f"安全状态：{risk['risk_level']}，{risk['risk_reason']}；{confirm_question}"
    )

    detail = (
        f"第 {ring_no} 环掘进完成，统计时间为 {summary['start_time']} 至 {summary['end_time']}，"
        f"共 {summary['record_count']} 条记录，里程由 {summary['mileage_start']:.3f} 推进至 {summary['mileage_end']:.3f}，"
        f"推进净行程最大值为 {summary['stroke_max']}。本环推进速度设定均值为 {fmt_num(summary['avg_speed_set'])}，"
        f"智控推进泵流量设定均值为 {fmt_num(summary['avg_smart_pump_set'], keep_decimal=True)}；"
        f"平均总推力 {fmt_num(summary['avg_thrust'])}，最大总推力 {summary['max_thrust']}，"
        f"平均刀盘扭矩 {fmt_num(summary['avg_torque'])}，最大刀盘扭矩 {summary['max_torque']}，"
        f"左右土仓平均土压分别为 {fmt_num(summary['avg_earth_p_l'], 2)} 和 {fmt_num(summary['avg_earth_p_r'], 2)}。"
        f"姿态方面，前端水平偏差 {fmt_range(summary['h_front_min'], summary['h_front_max'])}，"
        f"前端垂直偏差 {fmt_range(summary['v_front_min'], summary['v_front_max'])}，"
        f"后端水平偏差 {fmt_range(summary['h_tail_min'], summary['h_tail_max'])}，"
        f"后端垂直偏差 {fmt_range(summary['v_tail_min'], summary['v_tail_max'])}。\n"
        f"安全状态判定为{risk['risk_level']}：{risk['risk_reason']}。"
        f"当前报警位字段未触发，风险判定依据为{risk['threshold_source']}。\n"
        f"{confirm_question}"
    )

    posture_summary = (
        f"前端水平 {fmt_range(summary['h_front_min'], summary['h_front_max'])}、"
        f"前端垂直 {fmt_range(summary['v_front_min'], summary['v_front_max'])}、"
        f"后端水平 {fmt_range(summary['h_tail_min'], summary['h_tail_max'])}、"
        f"后端垂直 {fmt_range(summary['v_tail_min'], summary['v_tail_max'])}"
    )
    detailed = (
        f"【环总结】第 {ring_no} 环在 {summary['start_time']} 至 {summary['end_time']} 完成，"
        f"里程范围 {summary['mileage_start']:.3f}-{summary['mileage_end']:.3f}，推进净行程最大值 {summary['stroke_max']}，"
        f"关键参数为：推进速度设定均值 {fmt_num(summary['avg_speed_set'])}，平均总推力 {fmt_num(summary['avg_thrust'])}，"
        f"最大总推力 {summary['max_thrust']}，平均刀盘扭矩 {fmt_num(summary['avg_torque'])}，最大刀盘扭矩 {summary['max_torque']}，"
        f"左右土仓平均土压 {fmt_num(summary['avg_earth_p_l'], 2)}/{fmt_num(summary['avg_earth_p_r'], 2)}，"
        f"姿态偏差范围为{posture_summary}。\n\n"
        f"【安全警示】当前真实报警位未触发；根据{risk['threshold_source']}，本环{risk['risk_reason']}，"
        f"风险等级为{risk['risk_level']}。该风险等级仅用于 demo 展示，不替代现场安全裁决。\n\n"
        f"【现场问询】{confirm_question}如无异常，可记录为“已核对，无异常”。"
    )

    return {
        "brief": brief,
        "detail": detail,
        "detailed_explanation": detailed,
    }


def build_result(ring_no: int, sql_path: Path) -> Dict[str, Any]:
    rings = load_ring_aggs(sql_path)
    summaries = {ring_id: finalize_ring_summary(ring) for ring_id, ring in rings.items()}
    if ring_no not in summaries:
        available = f"{min(summaries)}-{max(summaries)}"
        raise ValueError(f"未查询到环号 AVBA10={ring_no} 的历史数据。当前可用环号范围：{available}")

    summary = summaries[ring_no]
    thresholds = build_thresholds(summaries)
    risk = evaluate_risk(summary, thresholds)
    messages = build_messages(summary, risk)
    return {
        "scene": "环总结 + 安全警示",
        "mode": ["brief", "detail", "detailed_explanation"],
        "summary": summary,
        "thresholds": thresholds,
        "risk": risk,
        "messages": messages,
        "data_source": {
            "file": str(sql_path.resolve()),
            "table": "mb_data_his_auto",
            "access_mode": "read_sql_file_only",
        },
    }


def write_outputs(result: Dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ring_no = result["summary"]["ring_no"]

    json_path = output_dir / f"ring_{ring_no}_broadcast.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = [
        f"# 第 {ring_no} 环播报 demo 输出",
        "",
        "## 简约版",
        "",
        result["messages"]["brief"],
        "",
        "## 完整版",
        "",
        result["messages"]["detail"],
        "",
        "## 详细说明版",
        "",
        result["messages"]["detailed_explanation"],
        "",
    ]
    md_path = output_dir / f"ring_{ring_no}_broadcast.md"
    md_path.write_text("\n".join(markdown), encoding="utf-8")


def main() -> int:
    demo_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="盾构播报机器人 demo：从 SQL 文件生成环总结 + 安全警示播报")
    parser.add_argument("--ring", type=int, default=DEFAULT_RING_NO, help="读取环号 AVBA10，默认 1028")
    parser.add_argument("--sql-file", default=str(demo_dir / DEFAULT_SQL_FILE), help="SQL 数据文件路径")
    parser.add_argument("--output-dir", default=str(demo_dir / "outputs"), help="输出目录")
    args = parser.parse_args()

    try:
        result = build_result(args.ring, Path(args.sql_file))
        write_outputs(result, Path(args.output_dir))
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1

    print("=== 简约版 ===")
    print(result["messages"]["brief"])
    print("\n=== 完整版 ===")
    print(result["messages"]["detail"])
    print("\n=== 详细说明版 ===")
    print(result["messages"]["detailed_explanation"])
    print(f"\n输出文件已生成：{Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
