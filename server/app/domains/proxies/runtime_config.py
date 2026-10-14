"""项目自有运行配置生成：订阅原文 → 内核实际加载的纯节点配置。

两处入口共用同一份节点依赖解析能力，不另立第二套：

- `render_subscription_config`：机场整份订阅 → 运行配置。**只留连接需要的东西**
  （节点、节点来源、必要的中转组、基础解析），剥掉规则引擎与地区分类；
- `resolve_node_dependencies`：把 `dialer-proxy` 的订阅原名映射成运行名
  （池渲染与恢复探测共用）。

设计前提：机场配置里的地区规则、rule-providers、地区 DNS 过滤、系统接管设置
都属于「那台机器上的通用客户端配置」，不是本项目要的形态——本项目自己生成
选择组、访问通道与解析设置，因此不消费 GeoIP/GeoSite 数据。缺目标不得静默
改成另一条连接路径，一律报错（fail-closed）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

# 运行配置里不保留的顶层键：规则引擎（规则/规则集/子规则）与本机接管面
# （tun），以及控制器凭据（本项目经命令行下发，配置文件里的一律不可信）。
_DROP_TOP_KEYS = frozenset({
    "rules",
    "rule-providers",
    "sub-rules",
    "tun",
    "geodata-mode",
    "external-controller",
    "external-ui",
    "secret",
    "mixed-port",
    "port",
    "socks-port",
    "redir-port",
    "tproxy-port",
})

# DNS 里会引地理数据或改写解析路径的键：地区过滤（fallback / fallback-filter）、
# 按域名定策略（nameserver-policy）、fake-ip 例外表都可能写 geosite/geoip。
_DNS_GEO_KEYS = frozenset({
    "fallback",
    "fallback-filter",
    "nameserver-policy",
    "fake-ip-filter",
    "geodata-mode",
})

# 纯节点来源订阅（无内联节点）需要一个组来装载 provider，否则节点不会被加载。
_PROVIDER_GROUP_NAME = "HlSubProviders"

# 内核自己解析的内置逻辑节点：`dialer-proxy` 指向它们属于合法用法（走直连/
# 拒绝），不经过订阅节点表，因此不做映射也不能判为「目标不存在」。
BUILTIN_PROXY_NAMES = frozenset({
    "DIRECT", "REJECT", "REJECT-DROP", "GLOBAL", "COMPATIBLE", "PASS", "PASS-RULE",
})


def resolve_node_dependencies(
    payloads: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
) -> list[dict]:
    """把节点名与 `dialer-proxy` 从订阅原名映射到运行名。

    `names` = 订阅原名 → 运行名的映射。返回新列表，**不改输入**。目标映射不到
    即抛 `ValueError`——静默剥离会让内核把这条连接换成另一条路径（例如直连），
    那是用户没要求的改动，比整份拒载更难排查。内置逻辑节点（DIRECT/REJECT…）
    原样放行：它们由内核自己解析，不属于订阅节点表。
    """
    resolved: list[dict] = []
    for payload in payloads:
        item = dict(payload)
        original_name = str(item.get("name") or "")
        item["name"] = names.get(original_name, original_name)
        target = item.get("dialer-proxy")
        if target:
            raw_target = str(target)
            mapped = names.get(raw_target)
            if not mapped and raw_target not in BUILTIN_PROXY_NAMES:
                raise ValueError(f"节点连接依赖的目标不存在：{raw_target}")
            if mapped:
                item["dialer-proxy"] = mapped
        resolved.append(item)
    return resolved


def _needed_groups(groups: list[Mapping], proxies: list[Mapping]) -> set[str]:
    """被 `dialer-proxy` 依赖到的组（含组间传递）。

    其余组属于机场原本的选择逻辑，本项目不用——保留它们等于把机场的分流意图
    悄悄带进运行配置。
    """
    by_name = {str(g.get("name")): g for g in groups if g.get("name")}
    needed: set[str] = set()
    frontier = [
        str(p.get("dialer-proxy"))
        for p in proxies
        if isinstance(p, Mapping) and p.get("dialer-proxy")
    ]
    while frontier:
        target = frontier.pop()
        if target in needed or target not in by_name:
            continue
        needed.add(target)
        for member in by_name[target].get("proxies") or []:
            name = str(member)
            if name in by_name and name not in needed:
                frontier.append(name)
    return needed


def _render_dns(raw: Any) -> dict:
    """解析设置：保留基础能力（上游、模式），清掉一切引地理数据的键。

    全部删除会连节点域名解析一起丢掉；全盘继承又会把地区过滤带回来。
    折中是保留白名单外的普通键、只去引 geo 的那几个，并固定解析模式。
    """
    out: dict = {}
    if isinstance(raw, Mapping):
        out = {k: v for k, v in raw.items() if k not in _DNS_GEO_KEYS}
    out["enhanced-mode"] = "redir-host"
    return out


def render_subscription_config(text: str) -> str:
    """机场整份配置 → 项目运行配置（YAML 文本）。

    保留：节点、节点来源（proxy-providers，含纯 provider 订阅）、中转组、
    基础解析。剥离：规则/规则集/子规则、tun 接管、控制器凭据、地区 DNS 过滤。

    `dialer-proxy` 指向的节点或组必须存在，否则抛 `ValueError`——缺依赖的配置
    交给内核只会被 `-t` 拒配，早点在生成阶段点名更好定位。
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"订阅配置不是合法 YAML：{exc}") from None
    if not isinstance(doc, Mapping):
        raise ValueError("订阅配置不是 YAML 映射")

    proxies = [p for p in (doc.get("proxies") or []) if isinstance(p, Mapping)]
    providers = dict(doc.get("proxy-providers") or {})
    if not proxies and not providers:
        raise ValueError("订阅配置里没有节点，也没有节点来源")

    groups = [g for g in (doc.get("proxy-groups") or []) if isinstance(g, Mapping)]
    needed = _needed_groups(groups, proxies)
    node_names = {str(p.get("name")) for p in proxies}

    for proxy in proxies:
        target = proxy.get("dialer-proxy")
        if (
            target
            and str(target) not in node_names
            and str(target) not in needed
            and str(target) not in BUILTIN_PROXY_NAMES
        ):
            raise ValueError(f"节点连接依赖不完整：{target}")

    out_groups = [dict(g) for g in groups if str(g.get("name")) in needed]

    if providers:
        used: set[str] = set()
        for group in out_groups:
            used.update(str(name) for name in (group.get("use") or []))
        missing = [name for name in providers if name not in used]
        if missing:
            out_groups.append({
                "name": _PROVIDER_GROUP_NAME,
                "type": "select",
                "use": missing,
            })

    doc_out: dict = {
        "mode": "global",
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "geo-auto-update": False,
        "dns": _render_dns(doc.get("dns")),
    }
    if proxies:
        doc_out["proxies"] = proxies
    if providers:
        doc_out["proxy-providers"] = providers
    if out_groups:
        doc_out["proxy-groups"] = out_groups

    return yaml.safe_dump(doc_out, allow_unicode=True, sort_keys=False)
