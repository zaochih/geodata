#!/usr/bin/env python3
"""把 v2fly 官方工具导出的明文列表转换成 mihomo/Stash 可用的 .mrs。

输入：--plain-dir 下的 <name>.txt（v2fly `--exportlists` 的输出，每行 `type:value[:@attr,@attr]`）
输出：--out-dir/geosite/<name>.mrs 和 <name>.list；regex.list 里的列表另出 <name>.classical.list

三份配置：
  geosite.list  直接转换的 geosite 名单（支持 name@attr）
  merge.list    多来源合并去重，见文件头注释
  regex.list    保留 regexp/keyword 的列表（格式同 geosite.list），输出 classical 文本
  geoip.list    官方 IP 段（可扣除子集），输出 geoip/<name>.mrs（ipcidr）
"""
import argparse
import ipaddress
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


def read_lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def read_merges(path: Path) -> dict[str, list[str]]:
    merges = {}
    for line in read_lines(path):
        target, _, sources = line.partition("=")
        merges[target.strip()] = [s.strip() for s in sources.split("+") if s.strip()]
    return merges


def parse_plain(path: Path):
    """yield (type, value, attrs)"""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        typ, _, rest = line.partition(":")
        value, _, attr_part = rest.partition(":@")
        attrs = {a.strip().lstrip("@") for a in attr_part.split(",")} if attr_part else set()
        yield typ, value, attrs


def export_names(names_file: Path, merge_file: Path) -> list[str]:
    """需要让 v2fly 工具导出的列表名（去掉 @attr，包含 merge.list 里的 geosite: 来源）。"""
    names = {spec.partition("@")[0] for spec in read_lines(names_file)}
    if merge_file.exists():
        for sources in read_merges(merge_file).values():
            names.update(s.removeprefix("geosite:") for s in sources if s.startswith("geosite:"))
    return sorted(names)


def plain_to_sets(path: Path, want_attr: str = ""):
    """返回 (suffixes, fulls, classical)，classical 是无法放进 domain 类型 mrs 的 DOMAIN-REGEX/DOMAIN-KEYWORD 行。"""
    suffixes, fulls, classical = set(), set(), set()
    for typ, value, attrs in parse_plain(path):
        if want_attr and want_attr not in attrs:
            continue
        if typ == "domain":
            suffixes.add(value)
        elif typ == "full":
            fulls.add(value)
        elif typ == "regexp":
            classical.add(f"DOMAIN-REGEX,{value}")
        elif typ == "keyword":
            classical.add(f"DOMAIN-KEYWORD,{value}")
    return suffixes, fulls, classical


def url_to_sets(url: str):
    with urllib.request.urlopen(url, timeout=120) as resp:
        text = resp.read().decode()
    suffixes, fulls = set(), set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (suffixes.add(line[1:]) if line.startswith(".") else fulls.add(line))
    return suffixes, fulls


def prune(suffixes: set[str], fulls: set[str]):
    """删除被父级后缀覆盖的条目。"""
    def covered(domain: str, allow_self: bool) -> bool:
        parts = domain.split(".")
        start = 0 if allow_self else 1
        return any(".".join(parts[i:]) in suffixes for i in range(start, len(parts)))

    return (
        {d for d in suffixes if not covered(d, allow_self=False)},
        {d for d in fulls if not covered(d, allow_self=True)},
    )


def emit(out: Path, name: str, suffixes: set[str], fulls: set[str], mihomo: str, note: str = "") -> bool:
    lines = sorted([f"+.{d}" for d in suffixes] + list(fulls))
    if not lines:
        print(f"[ERROR] {name}: 结果为空", file=sys.stderr)
        return False
    (out / f"{name}.list").write_text("\n".join(lines) + "\n")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("\n".join(lines) + "\n")
    subprocess.run([mihomo, "convert-ruleset", "domain", "text", tmp.name, str(out / f"{name}.mrs")], check=True)
    print(f"{name}: {len(lines)} 条{note}")
    return True


def read_geoips(path: Path) -> dict[str, tuple[list[str], list[str]]]:
    """`目标名 = 来源 [- 扣除的来源] + 来源 ...` → {目标名: (来源, 扣除的来源)}"""
    specs = {}
    for line in read_lines(path):
        target, _, sources = line.partition("=")
        add, sub = [], []
        for part in sources.split("+"):
            head, *rest = part.split(" - ")
            add.append(head.strip())
            sub += [r.strip() for r in rest]
        specs[target.strip()] = (add, sub)
    return specs


def url_to_networks(url: str) -> list:
    """文本（每行一个 CIDR）、Google 风格 JSON、`URL@键,键`（取 JSON 里指定键下的 CIDR 数组），或 `AS12345`（RIPEstat 查询该 AS 当前宣告的前缀）→ 网段列表。"""
    if url.upper().startswith("AS") and url[2:].isdigit():
        url = f"https://stat.ripe.net/data/announced-prefixes/data.json?resource={url.upper()}"
        with urllib.request.urlopen(url, timeout=120) as resp:
            return [ipaddress.ip_network(p["prefix"]) for p in json.load(resp)["data"]["prefixes"]]
    url, _, keys = url.partition("@")
    req = urllib.request.Request(url, headers={"User-Agent": "stash-rules-build"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode()
    if keys:
        # 顶层是 {键: [CIDR...]} 的 JSON（如 api.github.com/meta），`@键,键` 指定要取哪几个
        data = json.loads(text)
        items = [i for k in keys.split(",") for i in data[k]]
    elif text.lstrip().startswith("{"):
        items = [p.get("ipv4Prefix") or p.get("ipv6Prefix") for p in json.loads(text)["prefixes"]]
    else:
        items = [line.split("#")[0].strip() for line in text.splitlines()]
    return [ipaddress.ip_network(i, strict=False) for i in items if i]


def subtract(nets: list, holes: list) -> list:
    for hole in holes:
        rest = []
        for net in nets:
            if net.version != hole.version or not net.overlaps(hole):
                rest.append(net)
            elif not net.subnet_of(hole):
                rest += net.address_exclude(hole)
        nets = rest
    return nets


def emit_ip(out: Path, name: str, nets: list, mihomo: str, note: str = "") -> bool:
    v4 = list(ipaddress.collapse_addresses(n for n in nets if n.version == 4))
    v6 = list(ipaddress.collapse_addresses(n for n in nets if n.version == 6))
    lines = [str(n) for n in v4 + v6]
    if not lines:
        print(f"[ERROR] {name}: 结果为空", file=sys.stderr)
        return False
    (out / f"{name}.list").write_text("\n".join(lines) + "\n")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write("\n".join(lines) + "\n")
    subprocess.run([mihomo, "convert-ruleset", "ipcidr", "text", tmp.name, str(out / f"{name}.mrs")], check=True)
    print(f"{name}: {len(v4)} 条 IPv4 + {len(v6)} 条 IPv6{note}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", type=Path, default=Path("geosite.list"))
    ap.add_argument("--merges", type=Path, default=Path("merge.list"))
    ap.add_argument("--regex", type=Path, default=Path("regex.list"))
    ap.add_argument("--geoip", type=Path, default=Path("geoip.list"))
    ap.add_argument("--plain-dir", type=Path)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--mihomo", default="mihomo")
    ap.add_argument("--print-export-names", action="store_true", help="打印需要 v2fly 工具导出的列表名（逗号分隔）并退出")
    args = ap.parse_args()

    if args.print_export_names:
        print(",".join(export_names(args.names, args.merges)))
        return 0
    if not (args.plain_dir and args.out_dir):
        ap.error("--plain-dir 和 --out-dir 必填")

    out = args.out_dir / "geosite"
    out.mkdir(parents=True, exist_ok=True)
    ok = True
    regex_specs = set(read_lines(args.regex)) if args.regex.exists() else set()

    for spec in read_lines(args.names):
        base, _, want_attr = spec.partition("@")
        src = args.plain_dir / f"{base}.txt"
        if not src.exists():
            print(f"[ERROR] {spec}: {src.name} 不存在（上游没有这个列表或为空）", file=sys.stderr)
            ok = False
            continue
        suffixes, fulls, classical = plain_to_sets(src, want_attr)
        note = ""
        if classical and spec in regex_specs:
            lines = sorted(classical)
            (out / f"{spec}.classical.list").write_text("\n".join(lines) + "\n")
            note = f"（另输出 {len(lines)} 条 regexp/keyword 到 .classical.list）"
        elif classical:
            note = f"（丢弃 {len(classical)} 条 regexp/keyword）"
        ok &= emit(out, spec, suffixes, fulls, args.mihomo, note)

    if args.merges.exists():
        for target, sources in read_merges(args.merges).items():
            suffixes, fulls = set(), set()
            try:
                for source in sources:
                    if source.startswith("geosite:"):
                        s, f, _ = plain_to_sets(args.plain_dir / f"{source.removeprefix('geosite:')}.txt")
                    else:
                        s, f = url_to_sets(source)
                    print(f"  {target} ← {source.rsplit('/', 1)[-1]}: {len(s)} 后缀 + {len(f)} 精确")
                    suffixes |= s
                    fulls |= f
            except (OSError, ValueError) as e:
                print(f"[ERROR] {target}: {e}", file=sys.stderr)
                ok = False
                continue
            before = len(suffixes) + len(fulls)
            suffixes, fulls = prune(suffixes, fulls)
            ok &= emit(out, target, suffixes, fulls, args.mihomo, f"（合并后 {before} → 去重 {len(suffixes) + len(fulls)}）")

    if args.geoip.exists():
        ip_out = args.out_dir / "geoip"
        ip_out.mkdir(parents=True, exist_ok=True)
        for target, (add, sub) in read_geoips(args.geoip).items():
            try:
                nets = [n for url in add for n in url_to_networks(url)]
                holes = [n for url in sub for n in url_to_networks(url)]
            except (OSError, ValueError, KeyError) as e:
                print(f"[ERROR] {target}: {e}", file=sys.stderr)
                ok = False
                continue
            ok &= emit_ip(ip_out, target, subtract(nets, holes), args.mihomo, f"（扣除 {len(holes)} 段）" if holes else "")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
