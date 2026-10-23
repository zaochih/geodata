#!/usr/bin/env python3
"""按「规则行」语义把 PR 的 unified diff（stdin）合并进 <community-dir>/data，而不是按文本行号/上下文打补丁。

data/ 下的列表文件是无序的规则集合，所以逐条处理 diff 里的增删改：
  新增  目标里已有同一规则且属性已包含 → 跳过；否则插入（尽量放在 diff 里前一行之后）
  删除  目标里有完全相同的规则行才删；已不存在 → 跳过
  修改  按属性增量合并：去掉 PR 删除的 @attr、加上 PR 新增的 @attr，保留目标行里其他属性和行尾注释
        （上游同时给同一行加了别的 @attr 也不冲突）
只处理 data/ 下的文件；PR 删除整个文件的改动忽略。单条无法处理的改动只警告并跳过，不影响其他。

退出码：0 有改动写入；3 PR 的改动已全部体现在目标里（无需应用）；1 没有可处理的 data/ 改动。
"""
import re
import sys
from pathlib import Path

DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
TYPED = re.compile(r"^(domain|full|keyword|regexp|include|[a-z]+):")


class Rule:
    """一行规则：key 标识规则本身，attrs 是有序的 @属性列表，comment 是行尾注释。"""

    def __init__(self, raw):
        self.raw = raw.strip()
        body, _, comment = self.raw.partition(" #")
        self.comment = f"#{comment}" if comment else ""
        toks = body.split()
        if not toks or toks[0].startswith("#"):
            # 整行注释当成不带属性的规则，key 就是整行
            self.key, self.attrs = self.raw, []
            toks = [body.strip()] if body.strip() else []
        elif toks[0].startswith("include:"):
            # include 的 @attr 是过滤条件，不同属性是不同的规则，整行当作规则本身
            self.key, self.attrs = " ".join(toks), []
            toks = [self.key]
        else:
            r = toks[0] if TYPED.match(toks[0]) else f"domain:{toks[0]}"
            self.key = r
            self.attrs = [t for t in toks[1:] if t.startswith("@")]
        self.rule_tok = toks[0] if toks else ""

    def render(self, attrs=None, comment=None):
        attrs = self.attrs if attrs is None else attrs
        comment = self.comment if comment is None else comment
        return " ".join([self.rule_tok, *attrs] + ([comment] if comment else []))


def parse_diff(text):
    """返回 [(path, status, [block...])]，block = (anchor, removed_raw_lines, added_raw_lines)。"""
    files, cur = [], None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = DIFF_FILE.match(ln)
        if m:
            cur = {"path": m.group(2), "status": "modify", "blocks": []}
            files.append(cur)
        elif cur is not None and ln.startswith("new file mode"):
            cur["status"] = "new"
        elif cur is not None and ln.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif cur is not None and ln.startswith("rename from"):
            cur["status"] = "rename"
        elif cur is not None and (h := HUNK.match(ln)):
            old_n = int(h.group(1)) if h.group(1) is not None else 1
            new_n = int(h.group(2)) if h.group(2) is not None else 1
            anchor, rem, add = None, [], []

            def flush():
                nonlocal rem, add
                if rem or add:
                    cur["blocks"].append((anchor, rem, add))
                rem, add = [], []

            while old_n > 0 or new_n > 0:
                i += 1
                if i >= len(lines):
                    break
                body = lines[i]
                tag, rest = body[:1], body[1:]
                if tag == "-":
                    rem.append(rest)
                    old_n -= 1
                elif tag == "+":
                    add.append(rest)
                    new_n -= 1
                elif tag == "\\":
                    pass
                else:
                    flush()
                    if rest.strip():
                        anchor = rest
                    old_n -= 1
                    new_n -= 1
            flush()
        i += 1
    return files


class Target:
    def __init__(self, path):
        self.path = path
        self.existed = path.exists()
        text = path.read_text() if self.existed else ""
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.dirty = False

    def find(self, key):
        return [i for i, l in enumerate(self.lines) if l.strip() and Rule(l).key == key]

    def insert(self, text, anchor):
        pos = len(self.lines)
        if anchor is not None:
            for i, l in enumerate(self.lines):
                if l.strip() == anchor.strip():
                    pos = i + 1
                    break
        self.lines.insert(pos, text)
        self.dirty = True
        return text

    def save(self):
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self.lines) + "\n")


def apply_block(t, anchor, removed, added, stats, warn):
    rem, add = {}, {}
    for raw in removed:
        if raw.strip():
            r = Rule(raw)
            rem.setdefault(r.key, []).append(r)
    for raw in added:
        if raw.strip():
            r = Rule(raw)
            add.setdefault(r.key, []).append(r)

    last = anchor
    for key in list(rem) + [k for k in add if k not in rem]:
        olds, news = rem.get(key, []), add.get(key, [])
        for n in range(max(len(olds), len(news))):
            old = olds[n] if n < len(olds) else None
            new = news[n] if n < len(news) else None
            idx = t.find(key)
            if old and new:  # 修改：属性增量合并
                if not idx:
                    last = t.insert(new.render(), last)
                    stats["applied"] += 1
                    continue
                cur = Rule(t.lines[idx[0]])
                dropped = [a for a in old.attrs if a not in new.attrs]
                gained = [a for a in new.attrs if a not in old.attrs]
                attrs = [a for a in cur.attrs if a not in dropped]
                attrs += [a for a in gained if a not in attrs]
                if attrs == cur.attrs:
                    stats["noop"] += 1
                else:
                    t.lines[idx[0]] = cur.render(attrs)
                    t.dirty = True
                    stats["applied"] += 1
                last = t.lines[idx[0]]
            elif new:  # 新增
                if any(set(new.attrs) <= set(Rule(t.lines[i]).attrs) for i in idx):
                    stats["noop"] += 1
                    last = t.lines[idx[0]]
                else:
                    last = t.insert(new.render(), last)
                    stats["applied"] += 1
            else:  # 删除
                hit = [i for i in idx if set(Rule(t.lines[i]).attrs) == set(old.attrs)]
                if hit:
                    del t.lines[hit[0]]
                    t.dirty = True
                    stats["applied"] += 1
                elif idx:
                    warn(f"{t.path.name}: 上游已改动 `{old.raw}`，跳过删除")
                    stats["skipped"] += 1
                else:
                    stats["noop"] += 1


def main():
    community = Path(sys.argv[1]).resolve()
    data = (community / "data").resolve()
    files = parse_diff(sys.stdin.read())
    stats = {"applied": 0, "noop": 0, "skipped": 0}
    seen = False

    def warn(msg):
        print(f"::warning::{msg}")

    for f in files:
        path = (community / f["path"]).resolve()
        if not f["path"].startswith("data/") or data not in path.parents:
            continue
        seen = True
        if f["status"] in ("deleted", "rename"):
            warn(f"{f['path']}: 忽略{'删除' if f['status'] == 'deleted' else '重命名'}")
            stats["skipped"] += 1
            continue
        t = Target(path)
        if f["status"] == "new" and not t.existed:
            # 新文件原样写入（保留空行和注释）
            t.lines = [l for _, _, added in f["blocks"] for l in added]
            t.dirty = True
            stats["applied"] += len(t.lines)
            t.save()
            continue
        for anchor, removed, added in f["blocks"]:
            apply_block(t, anchor, removed, added, stats, warn)
        t.save()

    print(f"applied={stats['applied']} noop={stats['noop']} skipped={stats['skipped']}")
    if not seen:
        return 1
    return 0 if stats["applied"] else 3


if __name__ == "__main__":
    sys.exit(main())
