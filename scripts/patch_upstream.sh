#!/usr/bin/env bash
# 把上游未合并的 PR 和本地 extra/ 合并进 community/data。
# 用法：patch_upstream.sh <community-dir> <patches.list> <extra-dir> [summary-file]
# 单个 PR 失败只警告并跳过，脚本始终退出 0。依赖 gh 和 GH_TOKEN。
# summary-file（可选）写入实际应用的 PR，如 "PR #123, #456"；没有则不创建。
set -uo pipefail

community=$(cd "$1" && pwd)
patches=$2
extra=$3
summary=${4:-}
repo=v2fly/domain-list-community
applied=()

if [ -f "$patches" ]; then
  while read -r line || [ -n "$line" ]; do
    line=${line%%#*}
    line=$(echo "$line" | xargs)
    [[ $line == pr:* ]] || continue
    spec=${line#pr:}
    n=${spec%%@*}
    sha=
    [[ $spec == *@* ]] && sha=${spec#*@}

    if ! info=$(gh pr view "$n" -R "$repo" --json state,headRefOid --jq '[.state,.headRefOid]|@tsv' 2>&1); then
      echo "::warning::PR #$n 查询失败：$info"
      continue
    fi
    IFS=$'\t' read -r state head <<<"$info"
    case $state in
      MERGED) echo "::notice::PR #$n 已合并，可从 patches.list 移除"; continue ;;
      CLOSED) echo "::warning::PR #$n 已关闭且未合并，跳过"; continue ;;
    esac
    if [ -n "$sha" ] && [[ $head != "$sha"* ]]; then
      echo "::warning::PR #$n 的 head 已变（固定 ${sha}，现在 ${head:0:7}），请确认后更新 patches.list"
    fi

    if ! diff=$(gh pr diff "$n" -R "$repo" 2>&1); then
      echo "::warning::PR #$n 获取 diff 失败：$diff"
      continue
    fi
    if ! grep -q '^diff --git a/data/' <<<"$diff"; then
      echo "::warning::PR #$n 没有 data/ 下的改动，跳过"
      continue
    fi
    # 只应用 data/ 下的改动；git apply 是原子的，失败不会留下半截改动
    if (cd "$community" && printf '%s\n' "$diff" | git apply --check --include='data/*' - 2>/dev/null); then
      (cd "$community" && printf '%s\n' "$diff" | git apply --include='data/*' -)
      echo "已应用 PR #$n"
      applied+=("#$n")
    else
      echo "::warning::PR #$n 无法应用到当前上游，跳过"
    fi
  done <"$patches"
fi

# 先 PR 后 extra：PR 的 diff 依赖未修改的文件上下文
if [ -d "$extra" ]; then
  for f in "$extra"/*; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    { printf '\n# --- local extras ---\n'; cat "$f"; } >>"$community/data/$name"
    echo "已追加 extra/$name"
  done
fi

if [ -n "$summary" ] && [ ${#applied[@]} -gt 0 ]; then
  echo "PR ${applied[*]}" | sed 's/ #/, #/g; s/^PR, /PR /' >"$summary"
fi
exit 0
