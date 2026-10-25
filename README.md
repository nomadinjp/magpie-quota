# magpie-quota

供 agent 使用的 Magpie 剩余额度查询脚本。仅需 Python 3.10+，无第三方依赖。
直接查询网关的 `GET /v1/magpie/quotas`，复用 Magpie 已配置的供应商和登录状态。

```bash
python3 quota.py --list       # 查看可查询的供应商 ID
python3 quota.py codex        # 按唯一后缀查询
python3 quota.py zai
python3 quota.py remote-magpie/codex  # 使用完整 ID 消除歧义
python3 quota.py              # 查询全部
```

默认输出紧凑纯文本，每个供应商账户一行，空格分隔，无表头和解释：

```text
codex 5h=80% 7d=74%
zai mcp_month=92% 5h=99% 7d=99%
```

上述为格式示例，具体数值取决于网关读数。

- 数值表示剩余百分比；金额余额保留原始币种，例如 `balance='CNY 12.50'`。
- `unlimited` 表示不限额，`unknown` 表示未知，`0%` 表示用尽。没有任何可用额度时返回错误。
- 窗口名称统一为小写，空格和标点转为下划线；`5 hours` 缩写为 `5h`，`7 days` 为 `7d`。名称冲突时追加编号以区分。
- 供应商默认显示唯一后缀；如果网关存在相同后缀则显示完整 ID。`--list` 始终逐行输出完整 ID。
- 自动过滤内部网关 key（`~keys`）记录，所有输出格式和供应商列表均只包含供应商额度。
- 多账户分别成行，使用 `account=别名` 区分；没有唯一别名时使用 `account=1` 等查询结果内的序号。省略邮箱和密钥。
- 值中有空格时使用 shell 引号，换行等控制字符转义，保证每个账户只占一行；可用 Python 的 `shlex.split` 拆分字段。
- 只支持 Magpie 能返回额度的供应商。精确 ID 优先；查询后缀有歧义时报错。没有供应商时输出 `empty`。

额度可能来自 Magpie 缓存。读数超过 5 分钟，或网关标记缓存、延迟读取、读取失败时，即使默认精简输出也会保留状态：

```text
codex 5h=80% 7d=74% stale=true age=7200s
codex 5h=80% stale=true age=unknown error='upstream unavailable'
```

`age` 是读数距今的秒数；时间未知时为 `unknown`。延迟读取额外标记 `deferred=true`。
`stale=true` 表示读数可能过期，并不一定是请求失败；退出码仍根据是否存在错误决定。

需要重置时间、完整读取时间、计划到期时间和数量单位时使用 `--detail`：

```bash
python3 quota.py codex --detail
```

```text
codex 5h=80% provider=remote-magpie/codex read=2026-10-25T01:29:44Z 5h_reset=2026-10-25T04:50:40Z
```

详细模式中，`*_used` 为已用数量、`*_limit` 为总量、`*_unit` 为单位；网关提供时才出现。

需要结构化数据时使用 `--json`，保留单行 JSON 数组和完整供应商 ID：

```bash
python3 quota.py codex --json
python3 quota.py --list --json
```

JSON 模式保留 `remaining_pct`、`readAt`、`asOf`、`readDeferred`、`error` 等原有字段；`--detail` 仅影响纯文本输出。

默认连接 `http://127.0.0.1:3425`，已在当前 WSL → Windows Magpie 环境验证。
需要其他地址或网关认证时，通过环境变量设置：

```bash
export MAGPIE_URL=http://WINDOWS_HOST:3425
export MAGPIE_TOKEN=your-gateway-key
python3 quota.py codex
```

也可使用 `--url http://WINDOWS_HOST:3425`、`--timeout 10`。
地址应为网关根地址，不含 `/v1`。脚本直连网关，不使用 HTTP 代理环境变量。
未设置 `MAGPIE_TOKEN` 时使用本机 Magpie 接受的占位 Bearer 值；需要认证时应设置有效网关密钥。

退出码：`0` 查询成功；`1` 连接失败、供应商不存在、额度不可用或包含供应商错误；`2` 参数错误。
请求和参数错误以单行 `error='...'` 写入 stderr；使用 `--json` 时为 `{"error":"..."}`。
供应商错误保留在 stdout 对应记录中并返回 `1`，可能同时带有旧读数。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```
