#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_engine.py — 纯标准库单文件规则引擎

用法:
    python3 rule_engine.py 规则文件 状态文件
    python3 rule_engine.py --demo          运行内置演示(连锁/去重/循环/错误报告)

规则文件格式(每行一条,'#' 开头为注释):
    规则名 优先级 条件组合 动作1 [动作2 ...]

    - 优先级: 整数, 数值大者先尝试; 同优先级按定义顺序
    - 条件组合: 条件项之间用 '&'(与) 和 '|'(或) 连接, '&' 优先级更高, 不支持括号
      条件项形如  字段>值  字段<=值  字段==值  字段!=值  等
      特殊条件 '*' 或 'TRUE' 表示恒真
    - 动作: 字段=值, 可写多个(空白分隔); 值支持整数/浮点/布尔(true,false,on,off)/字符串

状态文件格式(每行一个):
    字段=值

示例规则:
    降温 10 温度>30&湿度<50 空调=on
    通风 20 空调==on|温度>40 风扇=on
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

TERM_RE = re.compile(r"^([^\s=<>!&|]+?)\s*(>=|<=|==|!=|>|<)\s*(?![<>=!])(.+)$")
ACTION_RE = re.compile(r"^([^\s=<>!&|]+?)\s*=\s*(.*)$")


def parse_value(text):
    """把动作/状态里的字面值解析为 bool/int/float/str。"""
    t = text.strip()
    low = t.lower()
    if low in ("true", "on", "yes"):
        return True
    if low in ("false", "off", "no"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def compare(left, op, right):
    """比较状态值与条件字面值; 非数值的大小比较抛 TypeError。"""
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    numeric = (int, float)
    if isinstance(left, numeric) and isinstance(right, numeric):
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
    raise TypeError(f"无法对非数值 {left!r} 与 {right!r} 做 '{op}' 比较")


def parse_condition(text):
    """解析条件组合为 OR-of-AND 结构: [[(字段, 运算符, 值文本), ...], ...]。"""
    if text in ("*", "TRUE", "true"):
        return []  # 恒真
    groups = []
    for or_part in text.split("|"):
        terms = []
        for term_text in or_part.split("&"):
            term_text = term_text.strip()
            if not term_text:
                raise ValueError("存在空的条件项(多余的 '&' 或 '|')")
            m = TERM_RE.match(term_text)
            if not m:
                raise ValueError(f"无法解析条件项 '{term_text}'")
            terms.append((m.group(1), m.group(2), m.group(3)))
        groups.append(terms)
    if not groups:
        raise ValueError("条件为空")
    return groups


@dataclass
class Rule:
    name: str
    priority: int
    cond_text: str
    cond: list          # OR-of-AND; 空列表表示恒真
    actions: list       # [(字段, 值文本), ...]
    lineno: int
    order: int          # 定义顺序
    enabled: bool = True


def parse_rules(text):
    """解析规则文本, 返回 (规则列表, 错误列表)。语法错误的规则被跳过。"""
    rules, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"第 {lineno} 行: 语法错误, 缺少优先级/条件/动作"
                          f"(格式: 规则名 优先级 条件 动作)")
            continue
        name, prio_text = parts[0], parts[1]
        try:
            priority = int(prio_text)
        except ValueError:
            errors.append(f"第 {lineno} 行: 优先级 '{prio_text}' 不是整数")
            continue
        if len(parts) < 3:
            errors.append(f"第 {lineno} 行: 缺少条件")
            continue
        if len(parts) < 4:
            errors.append(f"第 {lineno} 行: 缺少结果动作")
            continue
        cond_text = parts[2]
        try:
            cond = parse_condition(cond_text)
        except ValueError as exc:
            errors.append(f"第 {lineno} 行: 条件语法错误: {exc}")
            continue
        actions, bad = [], False
        for tok in parts[3:]:
            m = ACTION_RE.match(tok)
            if not m or m.group(2) == "":
                errors.append(f"第 {lineno} 行: 动作 '{tok}' 语法错误, 应为 字段=值")
                bad = True
                break
            actions.append((m.group(1), m.group(2)))
        if bad:
            continue
        rules.append(Rule(name, priority, cond_text, cond, actions, lineno, len(rules)))
    return rules, errors


def parse_state(text):
    """解析初始状态, 返回 (状态字典, 错误列表)。"""
    state, errors = {}, []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ACTION_RE.match(line)
        if not m or m.group(2) == "":
            errors.append(f"状态文件第 {lineno} 行: 语法错误, 应为 字段=值")
            continue
        state[m.group(1)] = parse_value(m.group(2))
    return state, errors


def check_fields(rules, state):
    """检查条件引用的字段是否存在(初始状态字段 + 各规则动作会写入的字段)。"""
    errors = []
    known = set(state)
    for r in rules:
        for f, _ in r.actions:
            known.add(f)
    for r in rules:
        missing = sorted({f for group in r.cond for f, _, _ in group} - known)
        if missing:
            errors.append(f"第 {r.lineno} 行: 规则 '{r.name}' 引用了不存在的状态字段: "
                          f"{', '.join(missing)}(该规则已跳过)")
            r.enabled = False
    return errors


class Engine:
    """规则引擎: 按优先级反复扫描触发, 带去重与循环检测。"""

    MAX_STEPS = 10000  # 兜底, 防止意外死循环

    def __init__(self, rules, state):
        self.rules = sorted((r for r in rules if r.enabled),
                            key=lambda r: (-r.priority, r.order))
        self.state = dict(state)
        self.version = 0        # 状态版本号, 每次实际变化 +1
        self.last_fired = {}    # 规则名 -> 上次触发时的版本号(去重依据)
        self.log = []           # [(Rule, [(字段, 旧值, 新值), ...]), ...]
        self.errors = []
        self.cycle_report = None

    def snapshot(self):
        return tuple(sorted(self.state.items()))

    def eval_cond(self, rule):
        if not rule.cond:
            return True
        for group in rule.cond:
            if all(f in self.state and compare(self.state[f], op, parse_value(raw))
                   for f, op, raw in group):
                return True
        return False

    def run(self):
        history = {self.snapshot(): 0}  # 状态快照 -> 当时的触发步数
        steps = 0
        while steps < self.MAX_STEPS:
            fired = False
            for rule in self.rules:
                if not rule.enabled:
                    continue
                # 去重: 上次触发后状态没有变化, 不再重复触发
                if self.last_fired.get(rule.name, -1) == self.version:
                    continue
                try:
                    if not self.eval_cond(rule):
                        continue
                except TypeError as exc:
                    self.errors.append(f"第 {rule.lineno} 行: 规则 '{rule.name}' "
                                       f"条件求值失败: {exc}(该规则已停用)")
                    rule.enabled = False
                    continue
                changes = []
                for f, raw in rule.actions:
                    new = parse_value(raw)
                    old = self.state.get(f, "<未定义>")
                    if f not in self.state or self.state[f] != new:
                        changes.append((f, old, new))
                        self.state[f] = new
                if changes:
                    self.version += 1
                self.last_fired[rule.name] = self.version
                self.log.append((rule, changes))
                steps += 1
                fired = True
                if changes:
                    snap = self.snapshot()
                    if snap in history:
                        # 状态重现 => 之后必然无限重复, 报告环路径
                        start = history[snap]
                        path = [r.name for r, _ in self.log[start:]]
                        self.cycle_report = (start, len(self.log), path)
                        return
                    history[snap] = len(self.log)
                break  # 触发后从头按优先级重新扫描(连锁)
            if not fired:
                return
        self.errors.append(f"超过最大触发步数 {self.MAX_STEPS}, 已强制停止")


def render(engine, parse_errors, field_errors, state_errors):
    out = ["== 状态变化过程 =="]
    if not engine.log:
        out.append("(无规则触发)")
    for i, (rule, changes) in enumerate(engine.log, 1):
        if changes:
            desc = ", ".join(f"{f}: {old!r} -> {new!r}" for f, old, new in changes)
        else:
            desc = "(状态无变化, 该规则之后被去重)"
        out.append(f"[步骤 {i}] 触发 {rule.name} (优先级 {rule.priority}): {desc}")

    out += ["", "== 最终状态 =="]
    if engine.state:
        for k in sorted(engine.state):
            out.append(f"{k} = {engine.state[k]!r}")
    else:
        out.append("(空)")

    if engine.cycle_report:
        start, end, path = engine.cycle_report
        ring = " -> ".join(path + [path[0]])
        out += ["", "== 循环检测 ==",
                f"检测到循环触发: 第 {end} 步后的状态与第 {start} 步后相同, 环路径: {ring}"]

    all_errors = parse_errors + state_errors + field_errors + engine.errors
    out += ["", "== 错误报告 =="]
    out += all_errors if all_errors else ["(无错误)"]
    return "\n".join(out)


def run_one(rules_text, state_text):
    state, state_errors = parse_state(state_text)
    rules, parse_errors = parse_rules(rules_text)
    field_errors = check_fields(rules, state)
    engine = Engine(rules, state)
    engine.run()
    return render(engine, parse_errors, field_errors, state_errors)


DEMO = [
    ("演示 1: 优先级 + 连锁触发 + 去重",
     """\
# 规则名 优先级 条件 动作
降温 10 温度>30&湿度<60 空调=on
通风 20 空调==on 风扇=on
节能 30 风扇==on&空调==on 模式=eco
空转 40 温度>0 温度=35
""",
     """\
温度=35
湿度=40
空调=off
风扇=off
模式=normal
"""),
    ("演示 2: 互为触发造成循环",
     """\
甲 10 p==0 p=1 q=1
乙 10 q==1 p=0 q=0
""",
     """\
p=0
q=0
"""),
    ("演示 3: 语法错误与未知字段报告",
     """\
好规则 10 温度>30 空调=on
缺动作 10 温度>30
缺条件 10
坏优先级 高 温度>30 空调=on
坏条件 10 温度>>30 空调=on
未知字段 10 气压>100 空调=on
""",
     """\
温度=35
空调=off
"""),
]


def demo():
    for i, (title, rules_text, state_text) in enumerate(DEMO):
        if i:
            print("\n" + "=" * 60 + "\n")
        print(f"### {title} ###")
        print(run_one(rules_text, state_text))


def main(argv):
    if len(argv) == 2 and argv[1] == "--demo":
        demo()
        return 0
    if len(argv) != 3:
        print(__doc__.strip())
        return 2
    try:
        with open(argv[1], encoding="utf-8") as fh:
            rules_text = fh.read()
        with open(argv[2], encoding="utf-8") as fh:
            state_text = fh.read()
    except OSError as exc:
        print(f"读取文件失败: {exc}", file=sys.stderr)
        return 1
    print(run_one(rules_text, state_text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
