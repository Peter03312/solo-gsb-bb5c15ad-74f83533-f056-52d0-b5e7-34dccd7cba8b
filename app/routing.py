"""路由核心：地址校验、规则集、逐跳解析（精确优先、域兜底）、整集校验。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")

MAX_DEPTH = 64  # 防御性上限；正常成环会先被链上重复地址检测拦截


def is_valid_address(value: str) -> bool:
    return bool(EMAIL_RE.match(value))


def is_valid_domain(value: str) -> bool:
    return bool(DOMAIN_RE.match(value))


def domain_of(address: str) -> str:
    return address.split("@", 1)[1]


@dataclass(frozen=True)
class Target:
    kind: str   # "alias"（内部别名，继续逐跳解析）| "mailbox"（终点邮箱，不再递归）
    value: str


@dataclass(frozen=True)
class Rule:
    match_type: str  # "exact" | "domain"
    match_key: str   # exact: 完整地址；domain: 域
    action: str      # "reject" | "route"
    targets: tuple[Target, ...]


class RuleSet:
    """一份规则快照（草稿或某个发布版本）。"""

    def __init__(self, rules: Iterable[Rule]):
        self.rules = list(rules)
        self.exact: dict[str, Rule] = {}
        self.domain: dict[str, Rule] = {}
        for r in self.rules:
            table = self.exact if r.match_type == "exact" else self.domain
            table[r.match_key] = r

    def lookup(self, address: str) -> Rule | None:
        """逐跳判定：先精确规则，再域兜底。"""
        rule = self.exact.get(address)
        if rule is not None:
            return rule
        return self.domain.get(domain_of(address))


class ResolutionError(Exception):
    """单分支失败：rejected / no_route / cycle。任何一处出现即整次解析失败。"""

    def __init__(self, kind: str, address: str, chain: list[str], message: str):
        super().__init__(message)
        self.kind = kind
        self.address = address
        self.chain = chain          # 到达失败点的完整路径（含失败点）
        self.message = message

    def detail(self) -> dict:
        return {
            "error": self.kind,
            "message": self.message,
            "address": self.address,
            "chain": self.chain,
        }


def resolve(address: str, ruleset: RuleSet, allow_reject: bool = False) -> dict[str, list[list[str]]]:
    """展开 address 的全部分支，返回 {终点邮箱: [到达路径, ...]}（终点去重）。

    allow_reject=True 用于发布前校验：拒收规则是合法配置，视为终止，不算失败。
    任一别名分支拒收 / 无路由 / 成环都会抛出 ResolutionError，调用方不得返回部分结果。
    """
    endpoints: dict[str, list[list[str]]] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def visit(addr: str, chain: tuple[str, ...]) -> None:
        if addr in chain:
            raise ResolutionError("cycle", addr, [*chain, addr], f"routing loop detected at {addr}")
        if len(chain) >= MAX_DEPTH:
            raise ResolutionError("cycle", addr, [*chain, addr], "routing chain exceeds max depth")
        rule = ruleset.lookup(addr)
        if rule is None:
            raise ResolutionError("no_route", addr, [*chain, addr], f"no route for {addr}")
        if rule.action == "reject":
            if allow_reject:
                return
            raise ResolutionError("rejected", addr, [*chain, addr], f"{addr} rejected by rule")
        next_chain = (*chain, addr)
        for target in rule.targets:
            if target.kind == "mailbox":
                path = (*next_chain, target.value)
                key = (target.value, path)
                if key not in seen:
                    seen.add(key)
                    endpoints.setdefault(target.value, []).append(list(path))
            else:
                visit(target.value, next_chain)

    visit(address, ())
    return endpoints


def validation_seeds(ruleset: RuleSet) -> list[str]:
    """需要验证可达性的起点：所有精确规则的地址 + 所有规则目标里的内部别名。

    域兜底规则作用于整个域，其目标均为具体地址、已包含在种子中；
    从全部种子出发解析即可覆盖所有规则目标的可解析性与成环检测。
    """
    seeds: set[str] = set(ruleset.exact.keys())
    for rule in ruleset.rules:
        for target in rule.targets:
            if target.kind == "alias":
                seeds.add(target.value)
    return sorted(seeds)


def validate_ruleset(ruleset: RuleSet) -> list[dict]:
    """发布前校验：所有规则目标可解析且无环。返回问题列表（空 = 通过）。"""
    problems: list[dict] = []
    for seed in validation_seeds(ruleset):
        try:
            resolve(seed, ruleset, allow_reject=True)
        except ResolutionError as exc:
            problems.append({"seed": seed, **exc.detail()})
    return problems


# ---------- 投递变化预览：当前草稿 vs 基线版本 ----------


def _outcome(address: str, ruleset: RuleSet) -> dict:
    """单个地址在一套规则下的完整解析结果，用于两侧对比。

    成功为去重后的终点及全部到达路径；失败为 rejected / no_route / cycle 详情
    （与 /resolve 的 422 detail 同构）。路径排序后比较，排列顺序不计差异。
    """
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        return {"status": "failed", "error": exc.detail()}
    return {
        "status": "ok",
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


def _fallback_sample(domain: str, taken: set[str]) -> str:
    """域兜底类别的示例地址：避开两侧全部精确规则地址与别名目标。"""
    suffix = 0
    while True:
        local = "preview-probe" if suffix == 0 else f"preview-probe-{suffix}"
        candidate = f"{local}@{domain}"
        if candidate not in taken:
            return candidate
        suffix += 1


def diff_rulesets(baseline: RuleSet, draft: RuleSet) -> dict:
    """比较两份规则快照（基线版本 vs 当前草稿）的投递结果，仅列出有变化的项。

    - 探测地址 = 两侧全部精确规则地址 ∪ 全部别名目标（与发布校验同一套种子），
      别名下游的变化因此会追溯到引用它的入口地址；
    - 两侧出现过域规则的每个域，排除前述具体地址后的兜底地址类别用一个
      避开全部具体地址的示例地址代表；
    - 终点、到达路径、拒收 / 无路由 / 成环任一不同即视为变化；路径排列顺序不计。
    """
    seeds = sorted(set(validation_seeds(baseline)) | set(validation_seeds(draft)))
    changed_addresses = []
    for address in seeds:
        before, after = _outcome(address, baseline), _outcome(address, draft)
        if before != after:
            changed_addresses.append({"address": address, "baseline": before, "draft": after})

    taken = set(seeds)
    changed_domain_fallbacks = []
    for domain in sorted(set(baseline.domain) | set(draft.domain)):
        sample = _fallback_sample(domain, taken)
        before, after = _outcome(sample, baseline), _outcome(sample, draft)
        if before != after:
            changed_domain_fallbacks.append(
                {"domain": domain, "sample_address": sample, "baseline": before, "draft": after}
            )
    return {"changed_addresses": changed_addresses, "changed_domain_fallbacks": changed_domain_fallbacks}
