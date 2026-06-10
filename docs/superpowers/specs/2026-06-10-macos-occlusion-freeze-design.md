# macOS 窗口遮挡冻帧 / 截图 stale 帧防护 —— 设计（#156 子问题 B）

- 日期：2026-06-10
- Issue：[#156](https://github.com/ClaymanTwinkle/godot-cli-control/issues/156)（聚合 issue，本 spec **仅覆盖子问题 B**；子问题 A「daemon stop 优雅退出」已由 PR #165 合并）
- 范围：消除 macOS 对被遮挡窗口的渲染节流导致的「录制 stale 帧产物报废 / 连续截图拿到旧画面」——三个互补子方案 B1（录制窗口置顶）+ B2（截图前强制渲染）+ B3（截图 stale 风险信号）。

## 背景与问题

macOS 对**被其它窗口遮挡**的窗口做渲染节流：主循环照走（`wait-prop` / `wait-frames` 正常返回、断言全过、`exit 0`），但 RenderingServer **不出新帧**。两个失败面，都是「流程全绿、产物报废」，单次翻车成本是整段录制 / 一串截图：

- **录制**：Movie Maker 照写 stale 帧，产出从头到尾一张静止画面的视频（体积异常小，~150KB vs 正常 2.4MB）——下游 XGame 两次 demo 实锤翻车。
- **截图**：`get_image()` 拿到最后一次绘制的旧画面，连续 `screenshot` 的 SHA 相同。

现行 workaround 全压用户侧：录制脚本里 `set /root always_on_top true` + osascript activate；截图前 `call /root grab_focus` + `wait-frames 1`。本设计把这些下沉为工具默认行为。

## 目标 / 非目标

**目标**
- B1：`daemon start --record` 默认置窗口 `always_on_top`（遮挡才致命、失焦无碍，置顶即根治），`--no-always-on-top` opt-out。
- B2：`screenshot` 服务端抓帧前 `RenderingServer.force_draw()`，即使窗口被遮挡也主动出新帧。
- B3：`screenshot` 信封带 `stale_suspect` 风险信号（本次内容与上次截图字节相同），把「假绿」变成「可检测」。
- 删除 SKILL.md 里对应的用户侧 workaround。

**非目标（明确不在本 spec）**
- 子问题 A（已合并）。
- 交互式（非录制）daemon 的强制置顶——只有 record 默认置顶；纯截图的遮挡由 B2 force_draw 兜（见决策 1）。
- 用 `--force-draw` 做成 opt-in flag——force_draw 定为**默认行为**（见决策 2）。
- 把 `stale_suspect` 升级成「确定 stale」的硬断言或自动重试——它只是风险提示（见决策 3）。

## 已定设计决策（已与用户确认）

1. **B1 仅 `--record` 置顶**，不覆盖交互式 daemon。issue 建议、最小惊扰；纯截图的遮挡 stale 由 B2 force_draw 治本。`--no-always-on-top` opt-out。**所有平台都设**（record 时）——遮挡是 macOS 病，但置顶在 Linux/Win 录制也无害，统一单分支 + 有 opt-out；headless 无窗口自然不生效。
2. **B2 force_draw 是默认行为**，不做成 opt-in flag——默认就该拿新帧，否则 agent 得记得加 flag、默认仍可能 stale。开销可接受（截图非高频）。
3. **B3 `stale_suspect` 是风险提示，不是错误断言**。语义诚实：游戏**真静止**（暂停 / 静态画面）时连续截图字节也相同 → `stale_suspect=true`，这是「内容与上次相同，**可能** stale 也可能真没变」，由 agent 决定是否重试 / force，工具不自作主张重试。
4. **一个 spec + 一个 PR**：B1（录制路径：daemon + bridge）与 B2/B3（截图路径：low_level_api）虽是两个触点，但同属「遮挡冻帧防护」主题、改动适中，plan 内分 task。

## 架构与数据流

```
B1 录制置顶（录制路径）
  daemon start --record [--no-always-on-top]
    daemon.py: record 且 always_on_top → Godot args += --game-bridge-always-on-top
    bridge _ready: _has_cli_flag("--game-bridge-always-on-top") → get_window().always_on_top = true

B2/B3 截图（截图路径，low_level_api.take_screenshot_async）
  非 dummy 路径：
    RenderingServer.force_draw()        ← B2：主动出帧，绕过遮挡节流
    await frame_post_draw + get_image   ← 现有循环（#61 transient 兜底保留）
    crop（--node）→ png_buffer
    h = png_buffer.hash()               ← B3
    if h == _last_screenshot_hash: response["stale_suspect"] = true
    _last_screenshot_hash = h
    （path 直写 / base64 两路都带 stale_suspect）
```

## 组件设计

### B1 — always_on_top（`daemon.py` + `cli.py` + `game_bridge.gd`）

- **`daemon.start()`**：新增 keyword-only 参数 `always_on_top: bool = True`。在构建 Godot args 处，`if record and always_on_top:` 追加 `--game-bridge-always-on-top`（非 record 时该参数无意义、不追加）。
- **`cli.py`**：`daemon start` 与 `run` 的 record-only 参数组（注册 `--record` / `--movie-path` / `--fps` 的同一处）加 `--no-always-on-top`（`action="store_false"`, `dest="always_on_top"`, `default=True`），传入 `daemon.start(always_on_top=ns.always_on_top)`。record-only flag（挂到 status/stop 会污染——与现有 record flag 同约定）。
- **`game_bridge.gd`**：`_ready()` 中（listen 前、窗口已存在）`if _has_cli_flag("--game-bridge-always-on-top"): get_window().always_on_top = true`。`get_window()` 在 headless 下返回主窗口对象但无可视效果——设置不抛错即可，失败不阻塞启动。
- **兼容**：旧 addon 不解析该自定义 flag → Godot 忽略未知 `--` arg，无害；老项目跑一次 `init` 同步 addon 后即获新行为。

### B2 — force_draw（`low_level_api.gd` `take_screenshot_async`）

- 在现有 dummy 检测（`var dummy := RenderingServer.get_rendering_device() == null`）之后、抓帧循环之前：`if not dummy: RenderingServer.force_draw()`。
- `force_draw()` 同步强制渲染一帧、绕过 macOS 遮挡节流；随后保留现有 `await RenderingServer.frame_post_draw` + `get_viewport().get_texture().get_image()` 循环（确保拿到 image + #61 transient 兜底）。
- dummy / headless 路径**不调** force_draw（无 GPU；现有 `process_frame` 推进路径不变）。

### B3 — stale_suspect（`low_level_api.gd`）

- 新增成员 `var _last_screenshot_hash: int = 0`（或用一个 sentinel 表示「尚无上次」，避免首张截图恰好 hash==0 误判——见错误处理）。
- 在 `png_buffer`（最终落盘 / 编码的字节，crop 之后）算出后：`var h := png_buffer.hash()`；`if _has_prev and h == _last_screenshot_hash: response["stale_suspect"] = true`；然后 `_last_screenshot_hash = h; _has_prev = true`。
- path 直写分支与 base64 分支都在算出 `png_buffer` 之后，统一加 `stale_suspect` 到 `response`（不同尺寸 / `--node` 区域的字节自然 hash 不同，不误报跨形态）。
- 仅在「与上一张内容相同」时**加** `stale_suspect: true` 字段；不同则不加该字段（保持信封精简，消费方按存在性判断）。

## 错误处理 / 兼容

- `get_window().always_on_top = true` 在不支持的平台 / headless 失败 → 不阻塞启动（设置语句本身不抛；若某平台抛，spec 不强求处理，按现状即可）。
- `force_draw()` 仅非 dummy 调；dummy 路径行为完全不变。
- `stale_suspect` 首张截图：用 `_has_prev` 标志（而非拿 `_last_screenshot_hash == 0` 当哨兵），避免首张恰好 hash 为 0 时误判。首张永不带 `stale_suspect`。
- `stale_suspect` 是**可选字段**，不破坏现有 screenshot 信封消费方（path/bytes/region/image 不变）。

## 测试策略（诚实面对遮挡难自动复现）

真·遮挡冻帧需要另一个窗口实际遮挡目标窗口，**无法在 CI / 单测里自动复现**。因此自动化测的是**机制**，「macOS 真不冻帧」靠本机手动验证：

- **B1 pytest**：`daemon.start(record=True, always_on_top=True)` → Godot args 含 `--game-bridge-always-on-top`；`always_on_top=False` → 不含；`record=False` → 不含。CLI `--no-always-on-top` → `ns.always_on_top is False` 并透传。
- **B1 GUT**：`game_bridge` 对 `--game-bridge-always-on-top` 的 `_has_cli_flag` 检测路径（窗口属性在 orphan-node GUT 下无真窗口可验，测 flag 检测）。
- **B2**：现有 windowed screenshot 验证（本机 macOS + CI 的「macOS e2e — windowed screenshot + GUT」job 在真 GPU 跑）确认加了 `force_draw()` 后 screenshot 仍产出有效图像（force_draw 不破坏抓帧）。dummy/headless GUT 路径不变、回归不破。
- **B3**：把 hash 比对 + `stale_suspect` 判定逻辑做成可独立测的形态；GUT / e2e 构造「连续两张相同字节 → `stale_suspect=true`」「内容不同 → 无该字段」「首张无该字段」。
- **本机手动验证（macOS，遵循本仓「e2e 本机必真跑」精神）**：录制时拖另一窗口遮挡 → 产物不再是静止单帧；截图前遮挡 → force_draw 后仍拿到新帧。手动验证结论写进 PR 描述。
- 测试执行遵循全局规则：委托 subagent（`model: sonnet`）跑，主会话只收精简结论；覆盖率用 `coverage run -m pytest`，门槛 80%；GUT 走 `run_gut.py`（需 `GODOT_BIN`）。

## 文档同步

- **SKILL.md 模板**（`python/godot_cli_control/templates/skill/SKILL.md`）：删 / 改录制章节的 `always_on_top`+osascript workaround 与截图章节的 `grab_focus`+`wait-frames` workaround，改述：record 默认置顶（`--no-always-on-top` opt-out）、screenshot 默认 force_draw、信封 `stale_suspect` 字段含义（风险提示，非确定 stale）。
- **CHANGELOG**（`addons/godot_cli_control/CHANGELOG.md` `[Unreleased]`）：`### Added`（`stale_suspect` 信封字段 / `--no-always-on-top` flag）+ `### Changed`（record 默认置顶 / screenshot 默认 force_draw）按性质归类。
- 仓内 `.claude/skills` 渲染副本用 CI `skill-render-drift` 官方命令重渲染（`COLUMNS=80` + Python 3.12）。
- 改模板后跑 `cli.format_full_help()` 不崩 + `pytest python/tests/test_skills_install.py`。

## CLAUDE.md 契约符合性自检

1. **JSON 信封**：screenshot response 加**可选** `stale_suspect`，成功信封结构不破。✅
2. **错误码三段制**：不新增码。✅
3. **退出码语义**：不变。✅
4. **shell canonical surface**：新增 `--no-always-on-top`（record-only flag，有对应行为）；force_draw / stale_suspect 是 `screenshot` 既有子命令的行为增强，无需新子命令。✅
5. **preflight**：`--no-always-on-top` 是布尔 flag，无需 preflight。✅
6. **大 payload**：`stale_suspect` 是布尔；screenshot 仍走服务端落盘（#149），force_draw 不增大 payload。✅
7. **SKILL.md 同步**：见「文档同步」。✅
8. **localhost-only / blacklist**：不碰 listen 地址 / method / property blacklist；always_on_top / force_draw 仅作用自身窗口与渲染，无 RCE 面。✅

## 风险与未来项

- **风险：`always_on_top` 干扰**——record 时窗口强制置顶可能挡住用户其它窗口；有 `--no-always-on-top` 逃生口，且仅 record 触发。
- **风险：`force_draw` 性能**——每次 screenshot 多一次强制渲染；截图非高频，可接受；若未来高频截图场景出现性能问题再评估 opt-out。
- **`stale_suspect` 误报**：游戏真静止时为 true 属预期（风险提示语义），不视为 bug。
- **测试覆盖诚实边界**：自动化只覆盖机制，遮挡冻帧的「真不冻」靠手动验证——这是 GUI 平台行为的固有限制，PR 描述需写明手动验证结论。
- **后续**：交互式 daemon 的遮挡防护、`force_draw` opt-out、`stale_suspect` 升级为自动重试——均 YAGNI，按需再开。
