# stash-rules

> [!NOTE]
>
> 本仓库为自用规则。欢迎您借鉴相关逻辑自行部署，但不建议直接引用本仓库中的文件。
>
> 仓库中的文件命名、链接规则并非固定。如遇由于本人需要而发生的更名、路径变动，恕不另行通知。

自用的 Stash 规则。分两部分：

## 手写规则 `rule-set/`

纯文本，`behavior: domain`，`format: text`。

| 文件 | 用途 |
|---|---|
| `ads-domain` | 补充的广告/跟踪域名 |
| `can-be-direct` | 需要直连的域名 |
| `others-should-be-proxied` | 需要走代理的域名 |

```yaml
url: https://raw.githubusercontent.com/zaochih/stash-rules/refs/heads/main/rule-set/ads-domain
```

## 自建 geosite `release` 分支

由 GitHub Action 每天用 [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) 官方工具构建（上游、`patches.list` 里的 PR 和 `merge.list` 的来源自上次发布后都没有更新时跳过），
列表名单见 `geosite.list`，产物：

```yaml
behavior: domain
format: mrs
url: https://raw.githubusercontent.com/zaochih/stash-rules/refs/heads/release/geosite/geolocation-cn.mrs
```

`name@attr` 形式（如 `apple@cn`）表示只取带该属性的条目。每个 `.mrs` 旁边有同名 `.list` 明文，方便排查。

### 为什么不直接用 MetaCubeX/meta-rules-dat

它用 Loyalsoldier/domain-list-custom 的解析器处理 v2fly 数据，该解析器不支持 `include:X @-!cn`（排除属性）语法，
导致 `geolocation-cn` 里 bytedance/feishu/tencent/alibaba/jd 等整块子列表缺失。
详见 Loyalsoldier/domain-list-custom#135。

### 合并生成的 `cn`

`merge.list` 把 v2fly 的 `tld-cn` + `geolocation-cn` 和 [ChinaMax](https://github.com/blackmatrix7/ios_rule_script) 合并，
并删除被父级后缀覆盖的子域名。ChinaMax 条目多但没有 `.cn` 顶级域，v2fly 则带有飞书/字节等完整的公司列表，二者互补。

### 上游补丁 `patches.list` 与 `extra/`

构建时在上游数据（`community/data/`）上先打补丁，`include`、`@attr` 仍由官方工具处理：

- `patches.list`：上游尚未合并的 PR，一行一个 `pr:12345`（可写 `pr:12345@abc1234` 固定 head sha，PR 有新提交时警告）。
  只应用 PR 中 `data/` 下的改动；PR 已合并会提示移除，已关闭或无法应用会警告并跳过，不会让构建失败。
- `extra/<列表名>`：追加到上游同名列表末尾（不存在则新建），用 v2fly 源语法（`domain:` / `full:` / `regexp:` / `include:` / `@attr`）。
  在所有 PR 之后应用；与上游重复的条目会被官方工具自动去重。

### regexp / keyword 的 classical 输出 `regex.list`

`regex.list` 里的列表（格式同 `geosite.list`）会额外输出 `<name>.classical.list`，每行 `DOMAIN-REGEX,...` 或 `DOMAIN-KEYWORD,...`，
可作为 `behavior: classical`、`format: text` 的 provider 使用，放在对应 `.mrs` 旁边：

```yaml
behavior: classical
format: text
url: https://raw.githubusercontent.com/zaochih/stash-rules/refs/heads/release/geosite/category-game-platforms-download.classical.list
```

### IP 段 `geoip.toml`

从官方来源生成 `ipcidr` 类型的 mrs（目前有 `telegram`、`google`、`github`、`apple`）。`apple` 没有官方订阅，用 ARIN/APNIC 的注册信息和 Apple 官方文档，不展开 AS（BGP 宣告可能被污染），产物在 `release` 分支的 `geoip/` 下。`google` 用 `goog.json` 扣掉 `cloud.json`，只保留 Google 自家服务，不含 GCP 客户的地址。
用于域名嗅探不到、直接连 IP 的场景（如 Telegram），规则里记得带 `no-resolve`：

```yaml
behavior: ipcidr
format: mrs
url: https://raw.githubusercontent.com/zaochih/stash-rules/refs/heads/release/geoip/telegram.mrs
```

### 限制

- `regexp:` 和 `keyword:` 规则无法放进 `domain` 类型的 mrs；不在 `regex.list` 里的列表构建时会丢弃并在日志里提示数量，
  在 `regex.list` 里的则输出到 `.classical.list`。`geolocation-!cn` 因含 `category-porn` 的大量正则，刻意不输出。
- `patches.list` / `extra/` 只能新增条目，不支持删除。
- `cn` 依赖 ChinaMax 的第三方数据，口径与 v2fly 的 `cn` 不同，条目约 11 万。

### 本地构建

```sh
names=$(python3 scripts/build.py --print-export-names)
git clone --depth 1 https://github.com/v2fly/domain-list-community community
gh auth login   # 首次需要，patch_upstream.sh 用 gh 取 PR
bash scripts/patch_upstream.sh community patches.list extra
(cd community && go run ./ --datapath=./data --outputdir=../plain --exportlists="$names")
python3 scripts/build.py --plain-dir plain --out-dir dist --mihomo mihomo
```
