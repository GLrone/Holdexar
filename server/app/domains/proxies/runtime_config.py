"""生成不消费地理数据的运行配置，解析同订阅连接依赖，并给最终运行配置做统一准入。"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

_DNS_BOOLEAN_KEYS = frozenset({
    "enable", "ipv6", "prefer-h3", "use-hosts", "use-system-hosts",
})
_DNS_RESOLVER_KEYS = frozenset({
    "nameserver", "default-nameserver", "proxy-server-nameserver", "direct-nameserver",
})

# 内核自己解析的内置逻辑节点：`dialer-proxy` 指向它们属于合法用法（走直连/
# 拒绝），不经过订阅节点表，因此不做映射也不能判为「目标不存在」。
BUILTIN_PROXY_NAMES = frozenset({
    "DIRECT", "REJECT", "REJECT-DROP", "GLOBAL", "COMPATIBLE", "PASS", "PASS-RULE",
})

_GEO_CONFIG_KEYS = frozenset({
    "geo-auto-update", "geo-update-interval", "geodata-mode", "geodata-loader",
    "geosite-matcher", "geox-url",
})
_GEO_RULE_TYPES = frozenset({
    "GEOIP", "GEOSITE", "SRC-GEOIP", "IP-ASN", "SRC-IP-ASN", "RULE-SET",
})
_LOGIC_RULE_TYPES = frozenset({"AND", "OR", "NOT"})
_GEO_SELECTOR_PREFIXES = ("geosite:", "geoip:", "ip-asn:", "rule-set:")

# 注入的探测 lane 组前缀：唯一允许使用 include-all 的执行组。
PROBE_LANE_GROUP_PREFIX = "HlProbeLane"
# 最终运行配置里允许存在的代理组类型：成员的选择只由程序决定。
_EXECUTION_GROUP_TYPES = frozenset({"select", "relay"})
# 来源订阅里可确定性降级为 select 的自动组（保留成员声明顺序，默认取首位）。
_DETERMINISTIC_DOWNGRADE_TYPES = frozenset({"url-test", "fallback"})
_GROUP_AUTONOMY_KEYS = (
    "url", "interval", "lazy", "timeout", "max-failed-times", "expected-status",
    "tolerance", "strategy", "filter", "exclude-filter", "exclude-type", "use",
    "include-all", "include-all-proxies", "include-all-providers",
)
_GROUP_MEMBER_FILTER_KEYS = ("filter", "exclude-filter", "exclude-type")
_EXTERNAL_UI_KEYS = ("external-ui", "external-ui-url", "external-ui-name")


def _geo_selector(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(_GEO_SELECTOR_PREFIXES)


def _contains_geo_selector(value: Any) -> bool:
    if _geo_selector(value):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_geo_selector(item) for item in value)
    return False


def _contains_geo_rule(value: Any) -> bool:
    if isinstance(value, str):
        if value.split(",", 1)[0].strip().upper() in _GEO_RULE_TYPES:
            return True
        return any(
            match.group(1).upper() in _GEO_RULE_TYPES
            for match in re.finditer(r"\(([A-Za-z][A-Za-z0-9-]*),", value)
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            rule_type = str(key).strip().upper()
            if rule_type in _GEO_RULE_TYPES:
                return True
            if rule_type in _LOGIC_RULE_TYPES or rule_type.lower() in {"type", "rule", "rules"}:
                if _contains_geo_rule(item):
                    return True
            elif isinstance(item, (Mapping, list, tuple)) and _contains_geo_rule(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_geo_rule(item) for item in value)
    return False


def _geo_database_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    name = PurePosixPath(value.replace("\\", "/")).name.lower()
    if name in {"geoip.db", "geoip.metadb"}:
        return True
    if name.endswith(".mmdb"):
        return True
    stem = name.removesuffix(".dat")
    return name.endswith(".dat") and (
        stem in {"geoip", "geosite", "asn", "country"}
        or stem.startswith(("geoip-", "geosite-", "asn-", "country-"))
    )


def _geo_database_reference(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = urlsplit(value).path if "://" in value else value
    return _geo_database_path(path)


def _parse_config(config: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(config, str):
        try:
            parsed = yaml.safe_load(config)
        except yaml.YAMLError as exc:
            raise ValueError(f"运行配置不是合法 YAML：{exc}") from None
    else:
        parsed = config
    if not isinstance(parsed, Mapping):
        raise ValueError("运行配置不是 YAML 映射")
    return parsed


def validate_geo_free_config(config: Mapping[str, Any] | str) -> None:
    """Reject runtime configurations that make Mihomo consume geographic data."""
    parsed = _parse_config(config)

    keys = {str(key).lower() for key in parsed}
    configured = sorted(keys.intersection(_GEO_CONFIG_KEYS))
    if configured:
        raise ValueError(f"运行配置包含地理数据加载设置：{configured[0]}")
    if parsed.get("rule-providers"):
        raise ValueError("运行配置包含可能读取地理数据的规则集")
    if parsed.get("sub-rules"):
        raise ValueError("运行配置包含可能读取地理数据的子规则")
    if _contains_geo_rule(parsed.get("rules")):
        raise ValueError("运行配置包含依赖地理数据的规则")

    providers = parsed.get("proxy-providers")
    if isinstance(providers, Mapping):
        for provider in providers.values():
            if isinstance(provider, Mapping) and any(
                _geo_database_reference(provider.get(field)) for field in ("path", "url")
            ):
                raise ValueError("节点来源指向地理数据库文件")

    _validate_sniffer(parsed)

    dns = parsed.get("dns")
    if not isinstance(dns, Mapping):
        return
    for key in (
        "nameserver", "default-nameserver", "proxy-server-nameserver",
        "direct-nameserver", "fallback",
    ):
        values = dns.get(key)
        if _geo_selector(values):
            raise ValueError(f"DNS {key} 指向地理数据规则")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            if any(_geo_selector(value) for value in values):
                raise ValueError(f"DNS {key} 指向地理数据规则")

    for key in ("nameserver-policy", "proxy-server-nameserver-policy"):
        policy = dns.get(key)
        if isinstance(policy, Mapping) and any(
            _geo_selector(selector) or _contains_geo_selector(resolvers)
            for selector, resolvers in policy.items()
        ):
            raise ValueError(f"DNS {key} 使用地理数据规则")

    fallback = dns.get("fallback")
    fallback_filter = dns.get("fallback-filter")
    if (
        isinstance(fallback, Sequence)
        and not isinstance(fallback, (str, bytes, bytearray))
        and fallback
    ):
        if not isinstance(fallback_filter, Mapping) or fallback_filter.get("geoip") is not False:
            raise ValueError("DNS fallback 默认启用地理数据库筛选")
    if isinstance(fallback_filter, Mapping):
        if fallback_filter.get("geoip") is True or fallback_filter.get("geosite"):
            raise ValueError("DNS fallback-filter 使用地理数据")
        if "geoip-code" in fallback_filter:
            raise ValueError("DNS fallback-filter 指定地理 IP 区域")

    fake_ip_filter = dns.get("fake-ip-filter")
    if _contains_geo_rule(fake_ip_filter) or _contains_geo_selector(fake_ip_filter):
        raise ValueError("DNS fake-ip-filter 使用地理数据规则")


def _validate_sniffer(parsed: Mapping[str, Any]) -> None:
    """Reject sniffer configurations that consume GeoSite data."""
    sniffer = parsed.get("sniffer")
    if not isinstance(sniffer, Mapping):
        return
    for field in ("force-domain", "skip-domain", "skip-src-address", "skip-dst-address"):
        if _contains_geo_selector(sniffer.get(field)):
            raise ValueError(f"sniffer.{field} 使用地理数据规则")


def _configured(value: Any) -> bool:
    return value not in (None, False, 0, "", [], {})


def validate_external_resource_config(config: Mapping[str, Any] | str) -> None:
    """Reject runtime configurations that let Mihomo fetch or update resources on its own."""
    parsed = _parse_config(config)
    if _configured(parsed.get("proxy-providers")):
        raise ValueError("运行配置包含未展开的节点来源，节点必须由程序获取后写成具体节点")
    for key in _EXTERNAL_UI_KEYS:
        if _configured(parsed.get(key)):
            raise ValueError(f"运行配置包含界面资源设置：{key}")


def validate_group_autonomy(config: Mapping[str, Any] | str) -> None:
    """Reject proxy groups through which Mihomo would probe or choose members by itself."""
    parsed = _parse_config(config)
    groups = parsed.get("proxy-groups")
    if groups is None:
        return
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes, bytearray)):
        raise ValueError("运行配置的代理组形态不对")
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("运行配置的代理组形态不对")
        name = str(group.get("name") or "")
        group_type = str(group.get("type") or "").lower()
        if group_type not in _EXECUTION_GROUP_TYPES:
            raise ValueError(
                f"代理组 {name} 的类型 {group_type or '未知'} 会让内核自行决定出口"
            )
        probe_lane = group_type == "select" and name.startswith(PROBE_LANE_GROUP_PREFIX)
        for key in _GROUP_AUTONOMY_KEYS:
            if not _configured(group.get(key)):
                continue
            if probe_lane and key == "include-all" and group.get(key) is True:
                continue
            raise ValueError(f"代理组 {name} 含内核自主选路或探活设置：{key}")


def validate_execution_config(config: Mapping[str, Any] | str) -> None:
    """Admission for every final kernel configuration: geo data, outside resources, group autonomy."""
    parsed = _parse_config(config)
    validate_geo_free_config(parsed)
    validate_external_resource_config(parsed)
    validate_group_autonomy(parsed)


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
            group_type = str(item.get("type") or "select").lower()
            if group_type in _DETERMINISTIC_DOWNGRADE_TYPES:
                group_type = "select"
            elif group_type not in _EXECUTION_GROUP_TYPES:
                raise ValueError(f"连接依赖组 {token} 的类型 {group_type} 无法确定性执行")
            if any(item.get(flag) for flag in (
                "include-all", "include-all-proxies", "include-all-providers", "use",
            )) and any(item.get(key) for key in _GROUP_MEMBER_FILTER_KEYS):
                raise ValueError(f"连接依赖组 {token} 使用节点过滤，无法确定性展开")
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
            item["type"] = group_type
            for autonomy_key in (
                "url", "interval", "lazy", "timeout", "max-failed-times", "expected-status",
                "tolerance", "strategy", *_GROUP_MEMBER_FILTER_KEYS,
            ):
                item.pop(autonomy_key, None)
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
                resolvers = [value for value in values if isinstance(value, str)]
                if resolvers:
                    out[key] = resolvers
    out["enhanced-mode"] = "redir-host"
    return out


def render_subscription_config(text: str) -> str:
    """机场整份配置 → 项目运行配置（YAML 文本）。

    保留：节点、中转组（自动组降为 select，成员顺序即默认选择）、基础解析。
    剥离：规则/规则集/子规则、tun 接管、控制器凭据、地区 DNS 过滤。
    节点来源（proxy-providers）必须已由程序展开，否则拒绝生成。

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
    if doc.get("proxy-providers"):
        raise ValueError("订阅配置含未展开的节点来源，须先由程序获取并展开为具体节点")
    if not proxies:
        raise ValueError("订阅配置里没有节点")

    groups = [g for g in (doc.get("proxy-groups") or []) if isinstance(g, Mapping)]
    bundle = render_node_bundle(proxies, {}, groups=groups)
    out_groups = bundle["proxy-groups"]

    doc_out: dict = {
        "mode": "global",
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "dns": _render_dns(doc.get("dns")),
    }
    doc_out["proxies"] = bundle["proxies"]
    if out_groups:
        doc_out["proxy-groups"] = out_groups

    validate_execution_config(doc_out)
    return yaml.safe_dump(doc_out, allow_unicode=True, sort_keys=False)


def apply_provider_overrides(
    nodes: Sequence[Mapping[str, Any]], provider_config: Mapping[str, Any],
    provider_name: str = "",
) -> list[dict]:
    override = provider_config.get("override") or {}
    if not isinstance(override, Mapping):
        raise ValueError(f"节点来源 {provider_name} 的覆盖配置无效")
    if override.get("override-expr"):
        raise ValueError(f"节点来源 {provider_name} 使用了当前运行配置不支持的表达式覆盖")
    prefixes = override.get("additional-prefix", "")
    suffixes = override.get("additional-suffix", "")
    if not isinstance(prefixes, str) or not isinstance(suffixes, str):
        raise ValueError(f"节点来源 {provider_name} 的名称覆盖无效")
    name_rules = override.get("proxy-name") or []
    if isinstance(name_rules, Mapping):
        name_rules = [name_rules]
    if not isinstance(name_rules, list) or any(
        not isinstance(rule, Mapping)
        or not isinstance(rule.get("pattern"), str)
        or not isinstance(rule.get("target"), str)
        for rule in name_rules
    ):
        raise ValueError(f"节点来源 {provider_name} 的名称替换无效")
    try:
        compiled_rules = [
            (re.compile(rule["pattern"]), rule["target"])
            for rule in name_rules
        ]
    except re.error as exc:
        raise ValueError(f"节点来源 {provider_name} 的名称替换无效") from exc

    resolved: list[dict] = []
    for source in nodes:
        if not isinstance(source, Mapping):
            continue
        node = copy.deepcopy(dict(source))
        name = f"{prefixes}{str(node.get('name') or '')}{suffixes}"
        for pattern, target in compiled_rules:
            replacement = re.sub(
                r"\$(\d+)",
                lambda match: rf"\g<{match.group(1)}>",
                target,
            )
            name = pattern.sub(replacement, name)
        if name:
            node["name"] = name
        for key, value in override.items():
            if key not in {
                "additional-prefix", "additional-suffix", "proxy-name",
                "override-expr",
            }:
                node[key] = copy.deepcopy(value)
        resolved.append(node)
    return resolved


def expand_provider_nodes(text: str, provider_nodes: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """Replace remote provider references with nodes resolved by the subscription snapshot."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("订阅配置不是合法 YAML") from exc
    if not isinstance(doc, dict):
        raise ValueError("订阅配置不是 YAML 映射")
    providers = doc.get("proxy-providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("订阅配置的节点来源无效")

    resolved_providers: dict[str, list[dict]] = {}
    for provider_name, provider_config in providers.items():
        if not isinstance(provider_config, Mapping):
            raise ValueError(f"节点来源 {provider_name} 的配置无效")
        resolved_providers[str(provider_name)] = apply_provider_overrides(
            provider_nodes.get(provider_name, ()), provider_config, str(provider_name),
        )

    proxies = [dict(node) for node in (doc.get("proxies") or []) if isinstance(node, Mapping)]
    proxies.extend(
        node
        for provider_name in providers
        for node in resolved_providers.get(str(provider_name), ())
    )
    groups = doc.get("proxy-groups") or []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            used_providers = list(group.get("use") or [])
            if group.pop("include-all-providers", False):
                used_providers.extend(providers)
            names = [
                str(node.get("name"))
                for provider_name in used_providers
                for node in resolved_providers.get(str(provider_name), ())
                if isinstance(node, Mapping) and node.get("name")
            ]
            current = [str(name) for name in (group.get("proxies") or [])]
            group["proxies"] = list(dict.fromkeys([*current, *names]))
            group.pop("use", None)

    doc.pop("proxy-providers", None)
    doc["proxies"] = proxies
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
