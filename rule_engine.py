#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_engine.py — 优先级 + 连锁触发规则引擎（纯 Python 标准库，单文件）

用法:
    python3 rule_engine.py 输入文件          # 从文件读取
    python3 rule_engine.py < 输入文件        # 从标准输入读取

输入格式（# 开头为注释，空行忽略）:

    [state]                                # 初始状态，每行: 字段 = 值
    power = off
    temp  = 35

    [rules]                                # 规则，每行: 规则名 优先级 条件 => 动作
    启动电源  10  power==off && temp>30     => power=on
    启动风扇  20  power==on  && fan==off    => fan=on; temp=28

条件语法: 字段 比较符 值；比较符为 == != > >= < <=；
          多个条件用 &&（与）/ ||（或）组合，&& 优先级更高，可用括号。
动作语法: 字段 = 值；多个动作用 ; 分隔。
值:       整数、浮点数、true/false、'引号字符串'/"引号字符串" 或裸词（视为字符串）。
          条件与动作的右值均按字面量处理，不做字段间比较。

退出码: 0 正常到达不动点；1 输入存在错误；2 检测到循环触发。
"""

import re
import sys
from dataclasses import dataclass, field

MAX_STEPS = 100_000  # 兜底上限；有限状态 + 去重理论上必然终止


class RuleSyntaxError(Exception):
    """条件表达式语法错误（行号由调用方补充）。"""


# ---------------------------------------------------------------- 字面量

def parse_literal(text):
    """把词法单元解析成 Python 值：bool / int / float / str。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


# ---------------------------------------------------------------- 条件解析

_TOKEN_RE = re.compile(r"""
      (?P<WS>\s+)
    | (?P<NUMBER>-?\d+(?:\.\d+)?)
    | (?P<STRING>'[^'\n]*'|"[^"\n]*")
    | (?P<OP>==|!=|>=|<=|>|<)
    | (?P<AND>&&)
    | (?P<OR>\|\|)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<IDENT>[^\s=<>!&|()'";]+)
""", re.VERBOSE)


def tokenize_condition(text):
    tokens, pos = [], 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() != pos:
            raise RuleSyntaxError(f"无法识别的字符 {text[pos:m.start()]!r}")
        pos = m.end()
        if m.lastgroup != "WS":
            tokens.append((m.lastgroup, m.group()))
    if pos != len(text):
        raise RuleSyntaxError(f"无法识别的字符 {text[pos:]!r}")
    return tokens


class _CondParser:
    """递归下降：or -> and -> atom；atom 为括号或 字段 比较符 值。"""

    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0
        self.fields = []  # 条件引用到的状态字段，用于"字段不存在"校验

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def advance(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        if not self.toks:
            raise RuleSyntaxError("条件为空")
        node = self.parse_or()
        kind, text = self.peek()
        if kind is not None:
            raise RuleSyntaxError(f"条件末尾有多余内容 {text!r}")
        return node, self.fields

    def parse_or(self):
        node = self.parse_and()
        while self.peek()[0] == "OR":
            self.advance()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_atom()
        while self.peek()[0] == "AND":
            self.advance()
            node = ("and", node, self.parse_atom())
        return node

    def parse_atom(self):
        kind, text = self.peek()
        if kind == "LPAREN":
            self.advance()
            node = self.parse_or()
            if self.peek()[0] != "RPAREN":
                raise RuleSyntaxError("括号不匹配，缺少 )")
            self.advance()
            return node
        if kind != "IDENT":
            raise RuleSyntaxError(
                f"在 {text!r} 附近缺少条件项" if text else "条件不完整")
        field_name = text
        self.advance()
        self.fields.append(field_name)
        kind, text = self.peek()
        if kind != "OP":
            raise RuleSyntaxError(f"字段「{field_name}」后缺少比较运算符")
        op = text
        self.advance()
        kind, text = self.peek()
        if kind not in ("NUMBER", "STRING", "IDENT"):
            raise RuleSyntaxError(f"比较符 {op} 后缺少比较值")
        self.advance()
        return ("cmp", field_name, op, parse_literal(text))


def parse_condition(text):
    return _CondParser(tokenize_condition(text)).parse()


def eval_condition(node, state):
    kind = node[0]
    if kind == "and":
        return eval_condition(node[1], state) and eval_condition(node[2], state)
    if kind == "or":
        return eval_condition(node[1], state) or eval_condition(node[2], state)
    _, field_name, op, value = node
    left = state[field_name]
    try:
        return _compare(left, op, value)
    except TypeError:
        # 类型不可比（如字符串与数字比大小）时退化为字符串比较，保证不崩溃
        return _compare(str(left), op, str(value))


def _compare(a, op, b):
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    raise AssertionError(op)


# ---------------------------------------------------------------- 动作解析

_FIELD_RE = re.compile(r"[^\s=<>!&|()'\";]+")


def parse_actions(text):
    """解析 'a = 1; b = off'，返回 (动作列表, 目标字段列表, 错误消息或None)。"""
    actions, fields = [], []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        fname, sep, fval = part.partition("=")
        fname, fval = fname.strip(), fval.strip()
        if not sep:
            return None, None, f"动作 {part!r} 缺少赋值符号 ="
        if not _FIELD_RE.fullmatch(fname):
            return None, None, f"动作目标 {fname!r} 不是合法字段名"
        if not fval:
            return None, None, f"字段「{fname}」的赋值缺少值"
        if fval.startswith("="):
            return None, None, "赋值应使用单个 =（疑似把 == 写进了动作）"
        actions.append((fname, parse_literal(fval)))
        fields.append(fname)
    if not actions:
        return None, None, "动作列表为空"
    return actions, fields, None


# ---------------------------------------------------------------- 规则

@dataclass
class Rule:
    name: str
    priority: int
    order: int                 # 定义顺序，同优先级时用它排序
    condition: tuple
    actions: list
    line_no: int
    cond_fields: list = field(default_factory=list)
    action_fields: list = field(default_factory=list)

    def apply(self, state):
        """执行动作，返回 [(字段, 旧值, 新值), ...]；状态没变则返回空列表。"""
        changes = []
        for fname, value in self.actions:
            old = state[fname]
            if old != value:
                state[fname] = value
                changes.append((fname, old, value))
        return changes


def parse_rule_line(line, line_no, order):
    """解析一行规则，返回 (Rule 或 None, [错误消息, ...])。"""
    m = re.match(r"^(\S+)(?:\s+(\S+)(?:\s+(.*))?)?$", line)
    name, prio_s, rest = m.group(1), m.group(2), m.group(3)
    if prio_s is None:
        return None, [f"规则「{name}」缺少优先级、条件与动作"]
    try:
        prio = int(prio_s)
    except ValueError:
        return None, [f"规则「{name}」的优先级不是整数: {prio_s!r}"]
    if rest is None:
        return None, [f"规则「{name}」缺少条件与结果动作"]
    cond_text, sep, act_text = rest.partition("=>")
    cond_text, act_text = cond_text.strip(), act_text.strip()
    if not sep:
        return None, [f"规则「{name}」缺少结果动作（未找到 => 分隔符）"]
    errors = []
    if not cond_text:
        errors.append(f"规则「{name}」缺少条件")
    if not act_text:
        errors.append(f"规则「{name}」缺少结果动作")
    if errors:
        return None, errors
    try:
        condition, cond_fields = parse_condition(cond_text)
    except RuleSyntaxError as exc:
        return None, [f"规则「{name}」条件语法错误: {exc}"]
    actions, action_fields, err = parse_actions(act_text)
    if err:
        return None, [f"规则「{name}」动作语法错误: {err}"]
    rule = Rule(name, prio, order, condition, actions, line_no,
                cond_fields, action_fields)
    return rule, []


# ---------------------------------------------------------------- 输入解析

def parse_input(text):
    """解析整个输入文件，返回 (state, rules, [(行号, 错误), ...])。"""
    state, rules, errors = {}, [], []
    section = None
    seen_state = seen_rules = False
    order = 0
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low == "[state]":
            section, seen_state = "state", True
            continue
        if low == "[rules]":
            section, seen_rules = "rules", True
            continue
        if section == "state":
            key, sep, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if not sep or not key:
                errors.append((line_no, f"状态行格式错误（应为 字段 = 值）: {line!r}"))
                continue
            if not val:
                errors.append((line_no, f"状态字段「{key}」缺少初始值"))
                continue
            if key in state:
                errors.append((line_no, f"状态字段「{key}」重复定义"))
                continue
            state[key] = parse_literal(val)
        elif section == "rules":
            order += 1
            rule, errs = parse_rule_line(line, line_no, order)
            errors.extend((line_no, e) for e in errs)
            if rule:
                rules.append(rule)
        else:
            errors.append((line_no, f"内容必须放在 [state] 或 [rules] 段内: {line!r}"))
    if not seen_state:
        errors.append((0, "缺少 [state] 初始状态段"))
    if not seen_rules:
        errors.append((0, "缺少 [rules] 规则段"))

    # 字段存在性校验：条件引用与动作目标都必须已在初始状态中定义
    known = set(state)
    for rule in rules:
        for fname in rule.cond_fields + rule.action_fields:
            if fname not in known:
                errors.append((rule.line_no,
                               f"规则「{rule.name}」引用了不存在的状态字段「{fname}」"))
    errors.sort(key=lambda e: e[0])
    return state, rules, errors


# ---------------------------------------------------------------- 引擎

class Engine:
    """
    执行策略：
    - 规则按 (优先级降序, 定义顺序升序) 排列，每轮从头扫描；
    - 一条规则触发后立即从头重扫（连锁触发时高优先级规则始终先被考虑）；
    - 去重：记录 (规则, 触发前状态指纹)，同一规则在状态没变时不会重复触发；
    - 循环检测：记录每次触发后的状态指纹，若新状态与历史上某状态相同，
      则两次相同状态之间触发的规则序列即为环路径，报告并停机。
    """

    def __init__(self, rules, state):
        self.rules = sorted(rules, key=lambda r: (-r.priority, r.order))
        self.state = dict(state)
        self.log = []        # (rule, changes 或 None)
        self._nochange_logged = set()  # 无变化触发只记录一次，避免刷屏
        self.cycle = None    # 环路径（规则名列表）或 None
        self.overflow = False

    def fingerprint(self):
        return tuple(sorted(self.state.items()))

    def run(self):
        fired = set()  # (规则定义序号, 触发前状态指纹) —— 去重
        history = [(None, self.fingerprint())]
        seen_at = {history[0][1]: 0}
        steps = 0
        while True:
            fired_any = False
            for rule in self.rules:
                if not eval_condition(rule.condition, self.state):
                    continue
                fp = self.fingerprint()
                if (rule.order, fp) in fired:
                    continue  # 同一规则在状态没变时不重复触发
                fired.add((rule.order, fp))
                changes = rule.apply(self.state)
                if changes or rule.order not in self._nochange_logged:
                    self.log.append((rule, changes))
                    if not changes:
                        self._nochange_logged.add(rule.order)
                steps += 1
                if steps > MAX_STEPS:
                    self.overflow = True
                    return
                if changes:
                    new_fp = self.fingerprint()
                    if new_fp in seen_at:
                        idx = seen_at[new_fp]
                        self.cycle = [h[0] for h in history[idx + 1:]] + [rule.name]
                        return
                    seen_at[new_fp] = len(history)
                    history.append((rule.name, new_fp))
                fired_any = True
                break  # 触发后从头重扫，保证优先级语义
            if not fired_any:
                return


# ---------------------------------------------------------------- 输出

def format_value(v):
    return repr(v) if isinstance(v, str) else str(v)


def print_state(title, state):
    print(f"====== {title} ======")
    if not state:
        print("  （空）")
    width = max((len(k) for k in state), default=0)
    for key, val in state.items():
        print(f"  {key:<{width}} = {format_value(val)}")


def print_errors(errors):
    print("====== 错误报告 ======")
    for line_no, msg in errors:
        where = f"第 {line_no} 行" if line_no else "全局"
        print(f"  [{where}] {msg}")
    print(f"共 {len(errors)} 个错误，未执行规则。")


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    state, rules, errors = parse_input(text)
    print_state("初始状态", state)
    print()
    if errors:
        print_errors(errors)
        return 1

    engine = Engine(rules, state)
    engine.run()

    print("====== 触发过程 ======")
    if not engine.log:
        print("  （没有规则被触发）")
    for i, (rule, changes) in enumerate(engine.log, 1):
        head = f"[{i}] 规则「{rule.name}」(优先级 {rule.priority}, 第 {rule.line_no} 行)"
        if changes:
            print(f"{head} 触发:")
            for fname, old, new in changes:
                print(f"      {fname}: {format_value(old)} -> {format_value(new)}")
        else:
            print(f"{head} 条件满足，但动作未改变状态（已去重，状态不变前不再触发）")
    print()

    if engine.cycle is not None:
        path = " -> ".join(engine.cycle + [engine.cycle[0]])
        print(f"!! 检测到循环触发，已停机: {path}")
        print("   （上述规则互相触发使状态回到历史状态，继续执行将无限循环）")
        print()
    if engine.overflow:
        print(f"!! 触发次数超过上限 {MAX_STEPS}，已强制停机")
        print()

    print_state("最终状态", engine.state)
    return 2 if engine.cycle is not None or engine.overflow else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
