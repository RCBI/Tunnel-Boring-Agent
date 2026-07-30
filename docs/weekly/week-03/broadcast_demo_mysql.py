#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
盾构播报机器人播报模板生成程序

功能：
1. 从 mb_data_auto 读取实时数据；
2. 生成“总结型”“预报型”“安全警示型”三类播报模板；
3. 每类播报分别导出 Markdown 和 JSON 文件。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pymysql


RING_FIELD = "AVBA10"
TIME_FIELD = "localtime1"

CORE_FIELDS = [
    "id",
    "localtime1",
    "AVBA10",
    "AVBA02",
    "ATBA02",
    "ATBS01",
    "AZVS01",
    "AZVS11",
    "AZVS12",
    "AZVS13",
    "AZVS14",
    "AZBD10",
    "AZBD11",
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

ALARM_LABELS = {
    "T00029": "土压异常",
    "T00030": "刀盘扭矩注意",
    "T00031": "刀盘回转异常",
    "T00037": "滚动异常",
    "T00038": "滚动趋势关注",
    "T00054": "非常停止",
    "T00067": "推进系统关注",
    "T00110": "注浆系统关注",
    "T00142": "其他系统关注",
}

SELECT_FIELDS = CORE_FIELDS + ALARM_FIELDS


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
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    fields: Dict[str, NumericAgg] = field(default_factory=dict)
    alarm_counts: Dict[str, int] = field(default_factory=lambda: {name: 0 for name in ALARM_FIELDS})
    auto_drive_valid_count: int = 0

    def metric(self, field_name: str) -> NumericAgg:
        if field_name not in self.fields:
            self.fields[field_name] = NumericAgg()
        return self.fields[field_name]

    def add_row(self, row: Dict[str, Any]) -> None:
        self.record_count += 1
        sample_time = row.get(TIME_FIELD)
        if isinstance(sample_time, datetime):
            self.start_time = sample_time if self.start_time is None else min(self.start_time, sample_time)
            self.end_time = sample_time if self.end_time is None else max(self.end_time, sample_time)

        for field_name in CORE_FIELDS:
            if field_name in ("id", TIME_FIELD, RING_FIELD):
                continue
            self.metric(field_name).add(row.get(field_name))

        if has_auto_drive_setting(row):
            self.auto_drive_valid_count += 1

        for alarm_field in ALARM_FIELDS:
            if is_alarm_active(row.get(alarm_field)):
                self.alarm_counts[alarm_field] += 1


def to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int_or_none(value: Any) -> Optional[int]:
    number = to_float_or_none(value)
    if number is None:
        return None
    return int(round(number))


def fmt_num(value: Any, digits: int = 1) -> str:
    number = to_float_or_none(value)
    if number is None:
        return "无数据"
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "待确认"
    return f"{value * 100:.1f}%"


def is_alarm_active(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value) != 0
    text = str(value).strip()
    return bool(text and text.upper() != "NULL" and text != "0")


def has_auto_drive_setting(row: Dict[str, Any]) -> bool:
    for field_name in ("AZVS01", "AZVS11", "AZVS12", "AZVS13", "AZVS14", "AZBD10", "AZBD11"):
        value = to_float_or_none(row.get(field_name))
        if value is not None and value != 0:
            return True
    return False


def duration_minutes(start_time: Optional[datetime], end_time: Optional[datetime]) -> Optional[float]:
    if not start_time or not end_time:
        return None
    seconds = (end_time - start_time).total_seconds()
    if seconds < 0:
        return None
    return round(seconds / 60, 1)


def agg_avg(ring: RingAgg, field_name: str, digits: int = 1) -> Optional[float]:
    value = ring.fields.get(field_name, NumericAgg()).avg
    return None if value is None else round(value, digits)


def agg_max_abs(ring: RingAgg, field_name: str, digits: int = 1) -> Optional[float]:
    metric = ring.fields.get(field_name, NumericAgg())
    candidates = [v for v in (metric.min_value, metric.max_value) if v is not None]
    if not candidates:
        return None
    return round(max(abs(v) for v in candidates), digits)


def finalize_ring_summary(ring: RingAgg) -> Dict[str, Any]:
    left_pressure = agg_avg(ring, "ABBA04", 2)
    right_pressure = agg_avg(ring, "ABBA02", 2)
    pressure_diff = None
    if left_pressure is not None and right_pressure is not None:
        pressure_diff = round(abs(left_pressure - right_pressure), 2)

    auto_drive_proxy_rate = None
    if ring.record_count:
        auto_drive_proxy_rate = ring.auto_drive_valid_count / ring.record_count

    active_alarms = {
        field_name: count
        for field_name, count in ring.alarm_counts.items()
        if count > 0
    }

    return {
        "ring_no": ring.ring_no,
        "record_count": ring.record_count,
        "start_time": ring.start_time.isoformat(sep=" ") if ring.start_time else None,
        "end_time": ring.end_time.isoformat(sep=" ") if ring.end_time else None,
        "duration_minutes": duration_minutes(ring.start_time, ring.end_time),
        "stroke_max": ring.fields.get("ATBA02", NumericAgg()).max_value,
        "mileage_start": ring.fields.get("AVBA02", NumericAgg()).min_value,
        "mileage_end": ring.fields.get("AVBA02", NumericAgg()).max_value,
        "avg_speed_setting": agg_avg(ring, "ATBS01", 1),
        "avg_total_thrust": agg_avg(ring, "ATBA03", 1),
        "max_total_thrust": ring.fields.get("ATBA03", NumericAgg()).max_value,
        "avg_cutter_torque": agg_avg(ring, "ACBA03", 1),
        "max_cutter_torque": ring.fields.get("ACBA03", NumericAgg()).max_value,
        "avg_right_pressure": right_pressure,
        "avg_left_pressure": left_pressure,
        "avg_pressure_diff": pressure_diff,
        "max_abs_front_horizontal": agg_max_abs(ring, "AVSA01", 1),
        "max_abs_front_vertical": agg_max_abs(ring, "AVSA02", 1),
        "max_abs_back_horizontal": agg_max_abs(ring, "AVSA11", 1),
        "max_abs_back_vertical": agg_max_abs(ring, "AVSA12", 1),
        "auto_drive_proxy_rate": auto_drive_proxy_rate,
        "auto_drive_proxy_basis": "以智控推进泵设定、智控分区设定、智控模型代码等记录是否有效作为临时计算口径；正式自动驾驶实现率仍需项目组定义。",
        "active_alarms": active_alarms,
    }


def threshold_pair(values: Iterable[Optional[float]], digits: int = 1) -> Dict[str, Optional[float]]:
    cleaned = [float(v) for v in values if v is not None]
    if not cleaned:
        return {"attention": None, "warning": None}
    avg = statistics.mean(cleaned)
    std = statistics.pstdev(cleaned) if len(cleaned) > 1 else 0.0
    return {
        "attention": round(avg + 2 * std, digits),
        "warning": round(avg + 3 * std, digits),
    }


def build_thresholds(summaries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[float]]]:
    return {
        "max_total_thrust": threshold_pair((s["max_total_thrust"] for s in summaries), 1),
        "max_cutter_torque": threshold_pair((s["max_cutter_torque"] for s in summaries), 1),
        "avg_pressure_diff": threshold_pair((s["avg_pressure_diff"] for s in summaries), 2),
        "max_abs_front_vertical": threshold_pair((s["max_abs_front_vertical"] for s in summaries), 1),
        "max_abs_back_vertical": threshold_pair((s["max_abs_back_vertical"] for s in summaries), 1),
    }


def compare_threshold(value: Optional[float], pair: Dict[str, Optional[float]]) -> str:
    if value is None:
        return "unknown"
    warning = pair.get("warning")
    attention = pair.get("attention")
    if warning is not None and value >= warning:
        return "warning"
    if attention is not None and value >= attention:
        return "attention"
    return "normal"


def evaluate_indicators(summary: Dict[str, Any], thresholds: Dict[str, Dict[str, Optional[float]]]) -> Dict[str, Any]:
    risk_items: List[Dict[str, Any]] = []

    checks = [
        ("max_total_thrust", "总推力", "负载"),
        ("max_cutter_torque", "刀盘扭矩", "负载"),
        ("avg_pressure_diff", "左右土压差", "土压"),
        ("max_abs_front_vertical", "前端垂直姿态偏差", "姿态"),
        ("max_abs_back_vertical", "后端垂直姿态偏差", "姿态"),
    ]

    for key, label, category in checks:
        level = compare_threshold(summary.get(key), thresholds.get(key, {}))
        if level != "normal":
            risk_items.append(
                {
                    "category": category,
                    "object": label,
                    "level": level,
                    "value": summary.get(key),
                    "threshold": thresholds.get(key, {}),
                }
            )

    for field_name, count in summary.get("active_alarms", {}).items():
        risk_items.append(
            {
                "category": "报警",
                "object": ALARM_LABELS.get(field_name, field_name),
                "level": "warning",
                "value": count,
                "threshold": "报警位触发",
            }
        )

    level_rank = {"normal": 0, "attention": 1, "warning": 2}
    max_level = "normal"
    for item in risk_items:
        if level_rank[item["level"]] > level_rank[max_level]:
            max_level = item["level"]

    drive_rhythm = "基本平稳"
    stroke = to_float_or_none(summary.get("stroke_max"))
    if stroke is None or stroke < 1000:
        drive_rhythm = "需结合环完成状态核对"

    posture_focus = any(item["category"] == "姿态" for item in risk_items)
    load_focus = any(item["category"] == "负载" for item in risk_items)
    pressure_focus = any(item["category"] == "土压" for item in risk_items)

    control_result = "姿态控制总体稳定"
    if posture_focus:
        control_result = "姿态控制存在关注项"

    load_pressure_result = "未见明显异常"
    if load_focus and pressure_focus:
        load_pressure_result = "负载和土压均需关注"
    elif load_focus:
        load_pressure_result = "负载需关注"
    elif pressure_focus:
        load_pressure_result = "土压均衡性需关注"

    next_attention = []
    if posture_focus:
        next_attention.append("姿态偏差延续情况")
    if pressure_focus:
        next_attention.append("开环初期土压稳定")
    if load_focus:
        next_attention.append("刀盘负载和总推力波动")
    if not next_attention:
        next_attention.append("开环初期土压与姿态稳定")

    return {
        "risk_level": {
            "normal": "正常",
            "attention": "关注",
            "warning": "警示",
        }[max_level],
        "risk_items": risk_items,
        "drive_rhythm": drive_rhythm,
        "control_result": control_result,
        "load_pressure_result": load_pressure_result,
        "next_attention_items": next_attention,
    }


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
        read_timeout=30,
        write_timeout=30,
    )


def quote_identifier(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"非法表名：{name}")
    return f"`{name}`"


def fetch_latest_realtime(conn, realtime_table: str) -> Dict[str, Any]:
    table = quote_identifier(realtime_table)
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {table} ORDER BY {quote_identifier(TIME_FIELD)} DESC, `id` DESC LIMIT 1")
        row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"实时表 {realtime_table} 中没有数据")
    return row


def fetch_history_rows(conn, history_table: str, ring_no: int) -> List[Dict[str, Any]]:
    table = quote_identifier(history_table)
    fields = ", ".join(quote_identifier(name) for name in SELECT_FIELDS)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT {fields} FROM {table} WHERE {quote_identifier(RING_FIELD)}=%s ORDER BY {quote_identifier(TIME_FIELD)} ASC",
            (ring_no,),
        )
        return list(cursor.fetchall())


def fetch_available_ring_range(conn, history_table: str) -> Dict[str, Any]:
    table = quote_identifier(history_table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT MIN({quote_identifier(RING_FIELD)}) AS min_ring, "
            f"MAX({quote_identifier(RING_FIELD)}) AS max_ring, "
            f"COUNT(DISTINCT {quote_identifier(RING_FIELD)}) AS ring_count "
            f"FROM {table}"
        )
        row = cursor.fetchone()
    return {
        "min_ring": to_int_or_none(row.get("min_ring")),
        "max_ring": to_int_or_none(row.get("max_ring")),
        "ring_count": row.get("ring_count"),
    }


def fetch_all_history_summaries(conn, history_table: str) -> List[Dict[str, Any]]:
    table = quote_identifier(history_table)
    fields = ", ".join(quote_identifier(name) for name in SELECT_FIELDS)
    aggs: Dict[int, RingAgg] = {}
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {fields} FROM {table} ORDER BY {quote_identifier(RING_FIELD)}, {quote_identifier(TIME_FIELD)}")
        for row in cursor.fetchall():
            ring_no = to_int_or_none(row.get(RING_FIELD))
            if ring_no is None:
                continue
            aggs.setdefault(ring_no, RingAgg(ring_no=ring_no)).add_row(row)
    return [finalize_ring_summary(aggs[key]) for key in sorted(aggs)]


def aggregate_rows(ring_no: int, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    agg = RingAgg(ring_no=ring_no)
    for row in rows:
        agg.add_row(row)
    return finalize_ring_summary(agg)


def summarize_realtime_row(row: Dict[str, Any]) -> Dict[str, Any]:
    ring_no = to_int_or_none(row.get(RING_FIELD))
    if ring_no is None:
        raise RuntimeError("实时表最新数据缺少环号 AVBA10")
    agg = RingAgg(ring_no=ring_no)
    agg.add_row(row)
    summary = finalize_ring_summary(agg)
    summary["source_time"] = row.get(TIME_FIELD)
    return summary


def choose_history_ring(
    conn,
    history_table: str,
    current_ring: Optional[int],
    requested_ring: Optional[int],
) -> Tuple[int, List[Dict[str, Any]], str]:
    ring_range = fetch_available_ring_range(conn, history_table)
    if requested_ring is not None:
        rows = fetch_history_rows(conn, history_table, requested_ring)
        if rows:
            return requested_ring, rows, "manual"
        raise RuntimeError(f"指定历史环 {requested_ring} 在 {history_table} 中没有数据")

    expected_previous = current_ring - 1 if current_ring is not None else None
    if expected_previous is not None:
        rows = fetch_history_rows(conn, history_table, expected_previous)
        if rows:
            return expected_previous, rows, "previous_by_realtime_ring"

    fallback_ring = ring_range["max_ring"]
    if fallback_ring is None:
        raise RuntimeError(f"历史表 {history_table} 中没有可用环数据")
    rows = fetch_history_rows(conn, history_table, fallback_ring)
    return fallback_ring, rows, "fallback_latest_history"


def risk_basis_text(risk_items: List[Dict[str, Any]]) -> str:
    if not risk_items:
        return "关键风险指标未超过当前关注阈值"
    parts = []
    for item in risk_items[:3]:
        threshold = item.get("threshold")
        if isinstance(threshold, dict):
            limit = threshold.get(item.get("level")) or threshold.get("attention") or threshold.get("warning")
            parts.append(f"{item['object']}为 {fmt_num(item.get('value'))}，参考阈值约 {fmt_num(limit)}")
        else:
            parts.append(f"{item['object']}触发{threshold}")
    return "；".join(parts)


def build_common_context(
    realtime_row: Dict[str, Any],
    summary: Dict[str, Any],
    indicators: Dict[str, Any],
) -> Dict[str, Any]:
    current_ring = to_int_or_none(realtime_row.get(RING_FIELD))
    summary_ring = summary["ring_no"]
    next_ring = summary_ring + 1

    auto_rate = summary.get("auto_drive_proxy_rate")
    auto_rate_text = "……" if auto_rate is None else "……"

    prev_brief_result = (
        f"掘进节奏{indicators['drive_rhythm']}，{indicators['control_result']}，"
        f"负载与土压{indicators['load_pressure_result']}"
    )

    return {
        "current_ring": current_ring,
        "summary_ring": summary_ring,
        "next_ring": next_ring,
        "auto_drive_rate_text": auto_rate_text,
        "prev_brief_result": prev_brief_result,
        "next_attention_text": "、".join(indicators["next_attention_items"]),
        "risk_basis": risk_basis_text(indicators["risk_items"]),
        "forecast_data_status": "地质……，线型……，自动驾驶率……",
    }


def build_previous_ring_output(context: Dict[str, Any], summary: Dict[str, Any], indicators: Dict[str, Any]) -> Dict[str, Any]:
    brief = (
        f"第 {context['summary_ring']} 环完成。推进净行程 {fmt_num(summary['stroke_max'])}，"
        f"速度设定 {fmt_num(summary['avg_speed_setting'])}，左右土压约 "
        f"{fmt_num(summary['avg_left_pressure'])}/{fmt_num(summary['avg_right_pressure'])}。"
        f"{indicators['control_result']}，{indicators['load_pressure_result']}；"
        f"本环需要关注的问题为{context['next_attention_text']}。"
    )
    detail = (
        f"【工况概况】第 {context['summary_ring']} 环已完成，推进净行程 {fmt_num(summary['stroke_max'])}，"
        f"里程约 {fmt_num(summary['mileage_end'], 3)}，推进速度设定 {fmt_num(summary['avg_speed_setting'])}。"
        f"当前土压左/右约 {fmt_num(summary['avg_left_pressure'])}/{fmt_num(summary['avg_right_pressure'])}，"
        f"总推力 {fmt_num(summary['avg_total_thrust'])}，刀盘扭矩 {fmt_num(summary['avg_cutter_torque'])}。\n\n"
        f"【控制效果】本环{indicators['drive_rhythm']}，{indicators['control_result']}。"
        f"自动驾驶实现率为 {context['auto_drive_rate_text']}。\n\n"
        f"【问题与关注】本环负载与土压{indicators['load_pressure_result']}，"
        f"需要关注{context['next_attention_text']}。如现场存在软硬不均、渗漏、异响、管片拼装或纠偏异常，"
        f"请补充现场反馈。"
    )
    return make_output_payload("总结型", context, summary, indicators, brief, detail)


def build_next_ring_output(context: Dict[str, Any], summary: Dict[str, Any], indicators: Dict[str, Any]) -> Dict[str, Any]:
    forecast_focus = context["next_attention_text"]
    brief = (
        f"下一环为第 {context['next_ring']} 环，需重点关注{forecast_focus}。"
        f"结合上一环{context['prev_brief_result']}，开环初期建议重点观察土压、姿态和负载变化；"
        f"地质……，线型……，自动驾驶率……。"
    )
    detail = (
        f"【启动前概况】上一环中{context['prev_brief_result']}。"
        f"因此下一环开始后，应重点核对上一环关注项是否延续，避免开环初期偏差继续扩大。\n\n"
        f"【下一环预报与关注】下一环为第 {context['next_ring']} 环，需重点关注{forecast_focus}。"
        f"地质情况为……，线型情况为……，自动驾驶率预期为……；开环初期优先观察土压建立、姿态变化和负载响应。\n\n"
        f"【安全提醒】若下一环出现土压突变、姿态偏差扩大、刀盘负载升高或报警位触发，"
        f"应优先提示现场人员介入判断，并由中枢记录处置过程。"
    )
    return make_output_payload("预报型", context, summary, indicators, brief, detail)


def build_safety_warning_output(context: Dict[str, Any], summary: Dict[str, Any], indicators: Dict[str, Any]) -> Dict[str, Any]:
    risk_level = indicators["risk_level"]
    risk_object = "、".join(item["object"] for item in indicators["risk_items"][:3]) or "关键监测指标"
    brief = (
        f"安全提示：第 {context['summary_ring']} 环{risk_object}处于{risk_level}状态，"
        f"{context['risk_basis']}。下一环需重点关注{context['next_attention_text']}，"
        f"必要时由现场人员或判断模块进一步确认。"
    )
    detail = (
        f"【安全警示】第 {context['summary_ring']} 环触发{risk_level}级提示，"
        f"风险对象为{risk_object}。\n\n"
        f"【阈值判断】报警事件状态为{format_alarm_status(summary.get('active_alarms', {}))}。\n\n"
        f"【现场判断】下一环或当前环建议关注{context['next_attention_text']}，"
        f"如现场存在异响、渗漏、管片拼装异常或司机感知异常，请补充反馈。"
        f"该条播报不代表最终安全裁决，请现场人员介入判断。"
    )
    return make_output_payload("安全警示型", context, summary, indicators, brief, detail)


def format_alarm_status(active_alarms: Dict[str, int]) -> str:
    if not active_alarms:
        return "未见已关注报警位触发"
    return "、".join(f"{ALARM_LABELS.get(name, name)} {count} 次" for name, count in active_alarms.items())


def make_output_payload(
    template_goal: str,
    context: Dict[str, Any],
    summary: Dict[str, Any],
    indicators: Dict[str, Any],
    brief: str,
    detail: str,
) -> Dict[str, Any]:
    return {
        "template_goal": template_goal,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trigger": {
            "type": "mysql_realtime_snapshot",
            "reason": "读取本地实时表最新一行作为当前播报目标数据",
            "current_ring_no": context["current_ring"],
            "summary_ring_no": context["summary_ring"],
            "next_ring_no": context["next_ring"],
            "history_source_mode": "realtime_row_as_target",
        },
        "input_data": {
            "source": "local_mysql_readonly",
            "realtime_table": "mb_data_auto",
            "history_table": "mb_data_his_auto",
            "summary_ring": summary["ring_no"],
            "record_count": summary["record_count"],
            "stroke_max": summary["stroke_max"],
            "avg_total_thrust": summary["avg_total_thrust"],
            "avg_cutter_torque": summary["avg_cutter_torque"],
            "avg_left_pressure": summary["avg_left_pressure"],
            "avg_right_pressure": summary["avg_right_pressure"],
            "avg_pressure_diff": summary["avg_pressure_diff"],
            "posture": {
                "max_abs_front_horizontal": summary["max_abs_front_horizontal"],
                "max_abs_front_vertical": summary["max_abs_front_vertical"],
                "max_abs_back_horizontal": summary["max_abs_back_horizontal"],
                "max_abs_back_vertical": summary["max_abs_back_vertical"],
            },
            "auto_drive_proxy_rate": summary["auto_drive_proxy_rate"],
            "auto_drive_proxy_basis": summary["auto_drive_proxy_basis"],
        },
        "processed_indicators": {
            "drive_rhythm": indicators["drive_rhythm"],
            "control_result": indicators["control_result"],
            "load_pressure_result": indicators["load_pressure_result"],
            "risk_level": indicators["risk_level"],
            "risk_items": indicators["risk_items"],
            "next_attention_items": indicators["next_attention_items"],
        },
        "messages": {
            "brief": brief,
            "detail": detail,
        },
        "missing_info": [
            "下一环地质资料正式接口",
            "下一环线型资料正式接口",
            "项目统一自动驾驶实现率定义",
            "安全阈值正式规则或专家确认值",
        ],
    }


def write_markdown(payload: Dict[str, Any], path: Path) -> None:
    msg = payload["messages"]
    content = f"""# {payload['template_goal']}

## 简约版

{msg['brief']}

## 详细版

{msg['detail']}
"""
    path.write_text(content, encoding="utf-8")


def write_outputs(outputs: Dict[str, Dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_map = {
        "previous_ring_summary": outputs["previous_ring_summary"],
        "next_ring_forecast": outputs["next_ring_forecast"],
        "safety_warning": outputs["safety_warning"],
    }
    for stem, payload in file_map.items():
        json_path = output_dir / f"{stem}.json"
        md_path = output_dir / f"{stem}.md"
        json_path.write_text(json.dumps(payload["messages"], ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(payload, md_path)


def build_outputs(args: argparse.Namespace) -> Dict[str, Dict[str, Any]]:
    with connect_mysql(args) as conn:
        realtime_row = fetch_latest_realtime(conn, args.realtime_table)
        summary = summarize_realtime_row(realtime_row)
        all_summaries = fetch_all_history_summaries(conn, args.history_table)
        thresholds = build_thresholds(all_summaries)
        indicators = evaluate_indicators(summary, thresholds)
        context = build_common_context(realtime_row, summary, indicators)

    return {
        "previous_ring_summary": build_previous_ring_output(context, summary, indicators),
        "next_ring_forecast": build_next_ring_output(context, summary, indicators),
        "safety_warning": build_safety_warning_output(context, summary, indicators),
    }


def parse_args() -> argparse.Namespace:
    program_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="盾构播报机器人 MySQL 直连版程序")
    parser.add_argument("--host", default="127.0.0.1", help="MySQL 主机地址")
    parser.add_argument("--port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--user", default="root", help="MySQL 用户名")
    parser.add_argument("--password", default="", help="MySQL 密码；本机 rcbi 当前为空")
    parser.add_argument("--database", default="rcbi", help="数据库名")
    parser.add_argument("--realtime-table", default="mb_data_auto", help="实时数据表")
    parser.add_argument("--history-table", default="mb_data_his_auto", help="历史数据表")
    parser.add_argument("--output-dir", default=str(program_dir / "outputs_mysql"), help="输出目录")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        outputs = build_outputs(args)
        output_dir = Path(args.output_dir)
        write_outputs(outputs, output_dir)
    except Exception as exc:
        print(f"程序运行失败：{exc}")
        return 1

    print("盾构播报机器人 MySQL 直连版程序已生成三类播报：\n")
    for key in ("previous_ring_summary", "next_ring_forecast", "safety_warning"):
        payload = outputs[key]
        print(f"【{payload['template_goal']}】")
        print(payload["messages"]["brief"])
        print()
    print(f"输出文件已生成：{Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
