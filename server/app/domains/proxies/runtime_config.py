"""生成不消费地理数据的运行配置，并解析同订阅连接依赖。"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

_DNS_BOOLEAN_KEYS = frozenset({
    "enable", "ipv6", "prefer-h3", "use-hosts", "use-system-hosts",
})
_DNS_RESOLVER_KEYS = frozenset({
    "nameserver", "default-nameserver", "proxy-server-nameserver", "direct-nameserver",
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
    """按同一依赖闭包解析节点运行名。"""
    originals = {runtime: original for original, runtime in names.items()}
    source_names = [originals.get(str(item.get("name") or ""), str(item.get("name") or ""))
                    for item in payloads]
    return render_node_bundle(payloads, names, source_names=source_names)["proxies"]


def render_node_bundle(
    payloads: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    groups: Sequence[Mapping[str, Any]] = (),
    namespace: str = "",
    roots: Sequence[str] | None = None,
    provider_names: Sequence[str] = (),
    provider_configs: Mapping[str, Mapping[str, Any]] | None = None,
    source_names: Sequence[str] | None = None,
) -> dict[str, list[dict]]:
    """按指定节点取依赖闭包；缺失、歧义和循环依赖均拒绝生成。"""
    aliases: dict[str, set[int]] = {}
    runtime_names: list[str] = []
    for index, payload in enumerate(payloads):
        payload_name = str(payload.get("name") or "")
        raw_name = str(source_names[index]) if source_names is not None else payload_name
        runtime_name = str(names.get(raw_name, payload_name))
        runtime_names.append(runtime_name)
        aliases.setdefault(raw_name, set()).add(index)

    group_index: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        if group.get("name"):
            group_index.setdefault(str(group["name"]), []).append(index)
    group_names = {
        name: f"HlRelay|{namespace}|{name}" if namespace else name for name in group_index
    }
    visiting: list[tuple[str, int]] = []
    visited: set[tuple[str, int]] = set()
    rendered_nodes: dict[int, dict] = {}
    rendered_groups: dict[int, dict] = {}
    providers = dict(provider_configs or {name: {} for name in provider_names})
    provider_index = {name: index for index, name in enumerate(providers)}

    def visit_provider(name: str) -> None:
        if name not in provider_index:
            raise ValueError(f"连接依赖的节点来源不存在：{name}")
        key = ("provider", provider_index[name])
        if key in visiting:
            raise ValueError(f"连接依赖存在循环：{name}")
        if key in visited:
            return
        visiting.append(key)
        provider = providers[name]
        override = provider.get("override") or {}
        for target in (provider.get("proxy"), override.get("dialer-proxy")):
            if target:
                visit(str(target))
        visiting.pop()
        visited.add(key)

    def visit(token: str) -> str:
        node_ids = aliases.get(token, set())
        group_ids = group_index.get(token, [])
        if len(node_ids) + len(group_ids) > 1:
            raise ValueError(f"连接依赖目标重名：{token}")
        if not node_ids and not group_ids:
            if token in BUILTIN_PROXY_NAMES:
                return token
            raise ValueError(f"连接依赖目标不存在：{token}")
        is_group = bool(group_ids)
        index = group_ids[0] if is_group else next(iter(node_ids))
        return visit_entry(is_group, index)

    def visit_entry(is_group: bool, index: int) -> str:
        token = str(groups[index]["name"]) if is_group else (
            str(source_names[index]) if source_names is not None
            else str(payloads[index].get("name") or "")
        )
        key = ("group" if is_group else "node", index)
        runtime_name = group_names[token] if is_group else runtime_names[index]
        if key in visiting:
            raise ValueError(f"连接依赖存在循环：{token}")
        if key in visited:
            return runtime_name
        visiting.append(key)
        item = copy.deepcopy(dict(groups[index] if is_group else payloads[index]))
        item["name"] = runtime_name
        if item.get("dialer-proxy"):
            item["dialer-proxy"] = visit(str(item["dialer-proxy"]))
        if is_group:
            members = list(item.get("proxies") or [])
            if item.get("include-all") or item.get("include-all-proxies"):
                members.extend(str(p.get("name") or "") for p in payloads)
            uses = list(item.get("use") or [])
            if item.get("include-all") or item.get("include-all-providers"):
                uses.extend(providers)
            for source in uses:
                visit_provider(str(source))
            if not members and not uses:
                raise ValueError(f"连接依赖组没有可用成员：{token}")
            if members:
                item["proxies"] = list(dict.fromkeys(visit(str(member)) for member in members))
            if uses:
                item["use"] = list(dict.fromkeys(uses))
            for flag in ("include-all", "include-all-proxies", "include-all-providers"):
                item.pop(flag, None)
            item.setdefault("empty-fallback", "REJECT")
            rendered_groups[index] = item
        else:
            rendered_nodes[index] = item
        visiting.pop()
        visited.add(key)
        return runtime_name

    for provider_name in providers:
        visit_provider(provider_name)
    if roots is None:
        for index in range(len(payloads)):
            visit_entry(False, index)
    else:
        for root in roots:
            visit(str(root))
    result = {
        "proxies": [rendered_nodes[i] for i in range(len(payloads)) if i in rendered_nodes],
        "proxy-groups": [rendered_groups[i] for i in range(len(groups)) if i in rendered_groups],
    }
    output_names = [item["name"] for entries in result.values() for item in entries]
    if len(output_names) != len(set(output_names)):
        raise ValueError("连接依赖的运行名称重复")
    return result


def _render_dns(raw: Any) -> dict:
    """仅保留基础解析白名单，策略与未知字段一律不进入运行配置。"""
    out: dict = {}
    if isinstance(raw, Mapping):
        for key in _DNS_BOOLEAN_KEYS:
            if isinstance(raw.get(key), bool):
                out[key] = raw[key]
        for key in _DNS_RESOLVER_KEYS:
            values = raw.get(key)
            if isinstance(values, list):
                resolvers = [value for value in values if isinstance(value, str)
                             and not any(marker in value.lower() for marker in ("geosite:", "geoip:"))]
                if resolvers:
                    out[key] = resolvers
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
    bundle = render_node_bundle(proxies, {}, groups=groups, provider_configs=providers)
    out_groups = bundle["proxy-groups"]

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
        doc_out["proxies"] = bundle["proxies"]
    if providers:
        doc_out["proxy-providers"] = providers
    if out_groups:
        doc_out["proxy-groups"] = out_groups

    return yaml.safe_dump(doc_out, allow_unicode=True, sort_keys=False)
