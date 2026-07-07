# Changelog

只记用户可感知的变更（feature / BREAKING / 行为修复）；纯 CI、内部重构、文档微调不入账，明细见 git log。
版本由 git tag 派生（hatch-vcs）。发版时 `release.sh` 会校验本文件：`[Unreleased]` 非空时拒绝打 tag，
加 `--roll-changelog` 自动把 `[Unreleased]` 滚动为新版本段（issue #140）。

## [Unreleased]

### Changed
- **SKILL.md 拆成多文件渐进披露结构，触发时上下文占用从 ~1850 行降到 ~160 行**：旧版单文件 SKILL.md 渲染后 1854 行 / ~142KB（其中 1195 行是 `{{cli_help}}` argparse 全量帮助注入），agent 每次触发 skill 都整体灌进上下文——违背本项目「不让大 payload 撑爆 agent 上下文」的 AI 友好契约。现拆为：核心 `SKILL.md`（quickstart / 退出码 / 单行命令目录 / 高频 pitfalls / 路由表，≤400 行有测试锁）+ `references/*.md` 六个主题文件（commands / error-codes / daemon-multi-instance / recording / python-and-pytest / pitfalls，agent 按需 Read）。`{{cli_help}}` 注入删除——agent 现场跑 `godot-cli-control <cmd> -h` 拿永远最新的帮助，顺带消灭「重渲染必须 COLUMNS=80 + Python 3.12」的 argparse 折行漂移坑（CI skill-render-drift 改整目录 diff，新增 `python -m godot_cli_control.skills_install` 一键重渲染入口）。`init` / `init --skills-only` 现在写整个 skill 目录；`--skills-no-clobber` 逐文件跳过已存在的（老单文件安装升级时缺失的 references/ 仍会补上）。旧项目跑一次 `godot-cli-control init --skills-only` 即升级。API 变更：`skills_install.render_skill(version)` 不再收 `cli_help` 参数，新增 `skills_install.skill_files(version)`。

### Fixed
- **`call` 对错类型「标量」实参不再假成功（#167 的标量侧补集）**：#167 只关了 `call` 的 **JSON Array → 复合 Variant** 侧假成功；错类型的标量实参（`set_name 42` 数字→StringName、`set_z_index '"abc"'` String→int 等）此前仍直接喂 `callv`，引擎喷 `Cannot convert argument` 返回 `null`，RPC 却假 `ok:true`/`result:null`——调用方以为方法生效实则没跑（与 #164/#167 同级数据风险，且 README/SKILL 已宣称「typed 方法假成功不再可能」，文档与实现脱节）。现连 daemon 前按方法声明签名对非 Array 实参也做类型守卫：镜像 Godot `Variant::can_convert_strict`，命中安全互转组（数值组 `bool`/`int`/`float`、字符串组 `String`/`StringName`/`NodePath`）透传，跨组（必 `Cannot convert`）`-32602` fail-loud。数值宽化（`float`→`int`）、`bool`→`int`、`String`→`String` 等合法调用零回归；`null` 实参保守透传（不在本 issue 新增拦截）。纯 addon 服务端行为改动，老项目跑一次 `init` 同步。
- **`plugin.cfg` 版本号不再僵在 `0.1.0`**：此前 addon 的 `plugin.cfg` 一直写死 `version="0.1.0"`，CI 仅在打包 AssetLib zip 时按 tag `sed` 修正，而 pip wheel 在那条 sed 之前就已构建——于是 `pip install godot-cli-control` + `init` 复制进下游项目的 addon、以及直接 clone addon 的用法，都拿到错误的 `0.1.0`（Godot 编辑器插件列表 / AssetLib 元数据里显示）。现把 committed 值对齐到上次发布版本，并让 `release.sh` 在打 tag 前自动同步 `plugin.cfg` 版本号并入库，三条取用路径（git-direct / pip+init / AssetLib）从此一致。纯元数据修正，不影响 CLI 行为（CLI 版本一直来自 hatch-vcs 的 `_version.py`）。

## [0.4.1] - 2026-06-14

### Fixed
- **#167 `call` 对 typed 参数不再假成功，且复用 `set` 的 Array→复合 Variant coerce**：此前 `handle_call_method` 裸 `node.callv(method, args)`，当方法形参是强类型（`Rect2`/`Vector2`/`Color` 等）而 CLI 传 JSON Array 时，Godot 只喷 `Cannot convert argument ...` 引擎错误并返回 `null`，RPC 仍假 `ok:true`/`result:null`——调用方以为方法生效实则没跑（#164 的 live `WANDER` 演示就栽在这）。GDScript 无 try-catch 抓不到 callv 运行期错误，现改为连 daemon 前按方法声明签名（`get_method_list()`）预校验：① JSON Array 喂复合 Variant 形参时按声明类型 coerce（`call /root/Mob enable_wander '[0,0,640,480]'` → `enable_wander(rect: Rect2)`），复用 `set` 那套 `_coerce_compound_array`（长度/元素错同样 `-32602` fail-loud）；② Array 喂标量/Object 形参（callv 必转换失败）→ `-32602` fail-loud；③ arg 数量越界（认可选/默认参数与 vararg）→ `-32602` fail-loud。标量实参（数字/字符串/布尔）与拿不到签名的内建方法保持原样透传，零回归。纯 addon 服务端行为改动，老项目跑一次 `init` 同步。

## [0.4.0] - 2026-06-12

### Added
- **`wait-signal --trigger`：同一连接 arm→触发→等信号，消灭 shell 后台三步模板与竞态（#155）**：`wait-signal <path> <signal> [timeout] --trigger '<subcommand>'` 在服务端 connect 信号处理器后先发 `armed` 中间帧，客户端收到后在同连接执行 trigger 子命令，再等信号终帧——arm 与触发之间零竞态窗口。命中信封新增 `trigger_result` 字段（trigger 子命令的返回值）。`--trigger` 接受任意 RPC 子命令（多步用 `combo`）；非 RPC 命令（`daemon`/`run`/`init`）及嵌套 `wait-*` 在 preflight 阶段（-1003/64）拒绝。需新 addon 能力（`arm_ack` / `armed` 帧协议）→ 老项目跑一次 `init` 同步。
- **坐标级鼠标注入 `click-at` / `mouse-move`**（#154）：`godot-cli-control click-at <x> <y> [--button left|right|middle] [--double]` 注入 down→up 鼠标点击，`mouse-move <x> <y>` 注入带 `relative` 的移动事件；两者都支持 `--node <path>` 取节点屏幕中心点（复用 `screenshot --node` 的坐标变换，hiDPI / stretch 窗口自洽）。坐标用 **viewport 物理像素**。区别于 `click`（节点级 UI 点击、需预知目标）：经 `Viewport.push_input` 走真实事件管线，能命中依赖光标位置的 `_gui_input` 控件（自定义 shape 的 `TextureButton`、`TouchScreenButton`、世界坐标 `Area2D` 拾取）。坐标与 `--node` 二选一、连接前 preflight 校验（-1003/64）。配套 `GameClient.click_at()/mouse_move()` 与 `GameBridge.click_at()/mouse_move()`。注意：事件的 `position`/`relative`/`button_mask` 对事件回调有效，但**不**更新 `Input` 单例的全局鼠标轮询（`get_global_mouse_position` / `is_mouse_button_pressed`）——读鼠标态请从事件参数读。需要新 RPC `click_at`/`mouse_move`——老 addon 项目跑一次 `init` 同步。
- **坐标级拖拽 `drag`**（#154 P2）：`godot-cli-control drag <x1> <y1> <x2> <y2> [--button left|right|middle] [--duration 0.3] [--steps 10]` 在起点按下鼠标键、按 `duration` 分 `steps` 段插值移动（移动事件全程带住按键 mask）、在终点松开——滑块拖动 / 拖放 / 滑动手势的原语。两端各自支持 `--from-node` / `--to-node` 取节点屏幕中心点（与该端坐标二选一，坐标数 0/2/4 按内容消歧，连接前 preflight 校验 -1003/64）。`duration` 是 game-time（受 `Engine.time_scale`，与 combo 同语义）；服务端 async 协程逐帧推进，客户端用长操作生死线（同 combo，受 `GODOT_CLI_LONG_OP_TIMEOUT`）。同一时刻只允许一个 drag 在途，再发回 `1014 DRAG_IN_PROGRESS`；`release-all` 会取消在途 drag 并补一个 mouse-up（避免被拖控件卡在「拖拽中」）。事件 `position`/`relative`/`button_mask` 对事件回调有效，但**不**更新 `Input` 单例轮询态（同 `click-at`/`mouse-move`）。配套 `GameClient.drag()` / `GameBridge.drag()`。需要新 RPC `drag`——老 addon 项目跑一次 `init` 同步。
- `tree` 新增可选 path 位置参数：`tree /root/GameUI [depth]` dump 任意子树，含 autoload 挂 `/root` 的兄弟（此前只能看 current_scene）；第一个参数以 `/` 开头当路径否则当 depth，`tree <depth>` 旧用法不变；路径不存在报 1001、用法错报 64（#150）。
- **`find` 服务端节点搜索**（#153）：`godot-cli-control find [--from <path>] [--type <Class>] [--exact <text>|--contains <substr>] [--name-pattern <glob>] [--limit N]`——单次 RPC 服务端遍历替代客户端 `children`+`text` 逐层递归（程序化匿名 UI `@Button@12` 按文本定位的原语；录制模式下每个 RPC 等帧渲染 50-150ms，一次全树遍历曾拖出 57s 死时间，现折成一次往返）。过滤器 AND 语义至少给一个（连接前 preflight，-1003/64）；`--type` 按继承匹配且认 `class_name` 脚本类；`--exact`/`--contains` 是 `text` 属性的精确/子串两档（互斥；精确档不叫 `--text` 因与全局输出 flag 撞名）；matches 按 BFS 浅层优先，超 `--limit`（默认 20，服务端上限 500）附 `truncated: true`（tree 同款信号）。退出码 0=有匹配、1=零匹配（shell `if` 可用）。配套 `GameClient.find_nodes()` / `GameBridge.find_nodes()`；entry 形状与 `tree` 对齐（`{name,type,path,text?,visible?}`）。需要新 RPC `find_nodes`——老 addon 项目跑一次 `init` 同步（未同步时报 `-32601`）。
- **screenshot `stale_suspect` 风险信号 + `--no-always-on-top`**（#156 子问题 B）：`screenshot` 信封新增可选 `stale_suspect: true`——本次截图字节与上一次完全相同（**可能** stale 也可能游戏真静止，风险提示非确定断言），agent 据此决定重试 / force。`daemon start --record` / `run --record` 新增 `--no-always-on-top`（不置顶，关）。
- **`run` 透传 `--time-scale`**（#157）：`run <script> --time-scale N` 与 `daemon start --time-scale` 对称，启动即设 `Engine.time_scale`（范围 (0,100]），透传给自动启动的 daemon——消除「run 模式只能在脚本首行 `bridge.time_scale(N)`」的不对称 workaround。
- **RPC 子命令的 `--port` / `--instance` 可后置**（#157）：`<subcommand> ... --port N` 与原来的 `--port N <subcommand>` 现在都接受（复用 `--json`/`--text` 的位置自由化机制，`default=argparse.SUPPRESS`），消灭「顶层 `--port` 必须写在子命令前」的 pitfall。`--port` 与 `--instance` 跨位置同给（一前一后）由 preflight guard 兜成 `-1003` / exit 64。
- **`emit-signal` 子命令 + `--allow-emit-signal` 逃生门**（#157 item4）：`godot-cli-control emit-signal <path> <signal> [args...]` 发射节点信号——测试里信号常是唯一接缝（如 `ItemList.select()` 不发 `item_selected`）。默认禁（服务端 `1015`），需 `daemon start` / `run` 带 `--allow-emit-signal` 显式 opt-in（debug-build + localhost 之上第三重门）。`emit_signal` 仍在方法黑名单里，`call <node> emit_signal` 始终被拒——只放开这一个目的明确、被门控的入口，不松动整张黑名单。args 同 `call`（逐个 JSON-or-string，`--text-value` 强制字符串）。需新 RPC `emit_signal`——老 addon 项目跑一次 `init` 同步（未同步时报 `-32601`）。配套 `GameClient.emit_signal()` / `GameBridge.emit_signal()`。

### Changed
- **`daemon stop` 优雅退出，录制尾帧不再丢**（#156 子问题 A）：`daemon stop` 现在在 SIGTERM 之前先经内部 `quit` RPC 让 Godot 正常退出，Movie Maker 在正常 quit 路径上 flush AVI 写缓冲——消除 SIGTERM 直杀丢 4-6s 尾帧的问题（下游录 demo 不必再垫 `wait(4.0)` 牺牲段）。RPC 不通（daemon 挂死 / 连不上 / 超时 / 进程不退）自动无缝降级原 SIGTERM→SIGKILL 路径，退出码语义不变。`quit` 是 stop 的内部 RPC，不单列 CLI 子命令。需要新 addon RPC——老项目跑一次 `init` 同步。
- **macOS 遮挡冻帧防护**（#156 子问题 B）：`daemon start --record` 现在默认置窗口 `always_on_top`（macOS 对被遮挡窗口节流渲染 → Movie Maker 写 stale 帧 / 截图拿旧画面，置顶根治；`--no-always-on-top` 关）；`screenshot` 服务端抓帧前 `RenderingServer.force_draw()` 主动出新帧绕过遮挡节流。下游不再需要 `set always_on_top` + osascript / `grab_focus` + `wait-frames` 用户侧 workaround。需要新 addon 行为——老项目跑一次 `init` 同步。
- **转码失败专用退出码 `4`**（#157）：`daemon stop` / `run` 在「进程已正常停止、原始 AVI 保留、仅 ffmpeg 转 mp4 失败」时返回 exit **4**（此前与连接失败 / daemon 起不来共用过载的 exit 2）——脚本可用 `rc==4` 直接判「只差转码」，exit 2 留给真 infra 故障。信封仍 `ok:true` + `daemon_stop_warning`；`daemon stop --all` 聚合不计此项（仍 `0|3`，3 只在硬 stop 失败时），转码失败只反映在 per-entry `rc:4`。
- **`get` sub-path leaf typo 对封闭复合类型 fail-loud**（#157 + #169）：`get <node> position:typo` 读封闭复合 Variant 的不存在 leaf 时现报 `1002 PROPERTY_NOT_FOUND`（message 列出合法 leaf），不再静默返回 `{"value": null}`（与真 null 不可区分的 footgun）。覆盖类型：Vector2/3/4（含 i 变体）、Color、Rect2/Rect2i、Transform2D/Transform3D、Basis、Plane、Quaternion、AABB、Projection——每类合法 leaf 集经 GUT 用 `get_indexed` 实证完整（基线 Godot 4.6.2），漏列即误杀合法读取故零猜测。嵌套子路径逐层校验（`transform:basis:typo`、`modulate:typo` 同样 fail-loud）。Dictionary/Array/Object 等开放/动态类型 key 不可枚举，保持现状 `get_indexed` 行为（零回归，误杀比静默更坏）。纯 addon 服务端行为改动，老项目跑一次 `init` 同步。
- **`set` sub-path leaf typo 对封闭复合类型 fail-loud**（#169 follow-up #174）：`set <node> position:zz 5` 写封闭复合 Variant 的不存在 leaf 时现报 `1002 PROPERTY_NOT_FOUND`（message 列出合法 leaf），不再静默丢值返 `{"success": true}`（agent 以为写成功实际没改的假成功 footgun）。复用 get 侧（#169）同一套 `_validate_sub_path_leaves` + `_SUBPATH_CLOSED_LEAVES`，覆盖类型与 leaf 集逐字相同，写前逐层校验、放行才 `set_indexed`；至此 set 与 get 行为对称。Dictionary/Array/Object 等开放/动态类型保持现状放行（零回归）。纯 addon 服务端行为改动，老项目跑一次 `init` 同步。

### Fixed
- **#172 `wait-signal --trigger` 的 `timeout` 不再被 trigger 执行时间吞掉**：此前 timeout 从服务端发出 `armed` 帧即开始计时，而 trigger 在客户端执行（往返 + 游戏内 duration）期间已在消耗预算——慢 trigger（`combo` / `drag`）可能在信号发射前提前误超时。现协议增强：客户端在 trigger 完成后发 `wait_signal_start_timer` 通知服务端，timeout 改为从 trigger 完成后才开始计——只覆盖「等信号」而非「trigger 执行 + 等信号」。`armed` 中间帧同时改用正向 `kind:"armed"` 判据识别（保留 `armed` 布尔字段给旧 client 前向兼容）。改动在 addon（game_bridge.gd / wait_api.gd）+ 客户端——老项目跑一次 `init` 同步 addon。
- **#151 `run` 的 GUI auto-detect 覆盖 sibling import 的 helper 模块**：`run <script>` 此前只 grep 主脚本源码判断是否含 `screenshot`，但 `run` 官方支持脚本同目录的 sibling import（`from helpers import shoot`，目录在 `sys.path` 上）——screenshot 调用写在 helper 模块里时检测漏报，非 TTY 下 daemon 仍以 headless 启动、跑到截图处静默 `1006` fail。现解析主脚本的 import、对**同目录命中的 .py** 一并做子串检测（只扫一层不递归；stdlib / 第三方包不在同目录，`read_text` OSError 自然跳过）。纯客户端检测改动，无需 `init` 同步 addon。
- **#160 `_send_json` 发送失败不再静默 → 假 `-1002` 超时**：单条响应超出站 WebSocket 缓冲（默认 10MB，`godot_cli_control/outbound_buffer_mb` 可调）时，`send_text` 返回值此前被丢弃、daemon 静默不响应，client await 挂到 30s 报 `-1002 Timeout`（真根因「响应过大」完全不可见，同 #149 的「误导性错误」家族）。现 `_send_json` 检查发送结果：失败时 stderr 留痕（payload 字节数 + buffer 上限），并对带 id 的响应补发一条小 `1016 RESPONSE_TOO_LARGE` 错误信封指路（screenshot 传 path 走 daemon 直写 / 调大 outbound_buffer_mb）；error 信封自身或无 id 的帧不补发（防递归）。触发面已被 #149 收窄到不传 path 的 bytes-API 巨图截图。新增服务端码 `1016`。改动在 addon（game_bridge.gd），老项目跑一次 `init` 同步即获此修复。
- **#149 大图 screenshot 不再误报 `-1001` 连接错误**：hiDPI / 4K 全屏截图的 base64 曾超出 WebSocket 客户端默认 1MB 消息上限，连接被 close 1009 关闭、却呈现为「随机连接失败」（暗色简单画面能过、复杂画面必挂）。三层修复：① 根治——`screenshot` 的 PNG 改由 **daemon 进程直接落盘**（CLI 把路径 resolve 成绝对路径并先建好父目录），图像字节不再过 WS，任意尺寸可截，顺带消灭 base64 编解码开销；旧 addon 不认新参数时自动回退本地写盘（跑一次 `init` 即同步）。② client 放开 `max_size` 上限（兜旧 addon 回退与 `bridge.screenshot()` bytes API）。③ 连接被关时错误信息带上 close code/reason，根因不再被笼统的 "Connection closed by server" 吞掉。新增服务端错误码 `1013 WRITE_FAILED`（daemon 写不进目标路径，区别于客户端 `-1004`）。`GameClient.screenshot_raw()` / `GameBridge.screenshot()` 新增 `path` 直写支持。截图前手动缩窗口的 workaround 可以删了。
- **#152 `--movie-path` 非 .avi/.png 时启动前拒绝（-1003 / exit 64）**：此前传 `.mp4` 等后缀 Godot 打 "Can't find movie writer" 后继续正常跑——脚本照常执行、exit 0，但什么都没录（假成功）。现 `daemon start` / `run` 的 `--record` 在 argparse 层校验扩展名（大小写不敏感），错误信息指路「传 .avi，stop 时自动转码出 .mp4」；直接调 `Daemon.start()` 的 API 路径同样拒绝（DaemonError）。

## [0.3.0] - 2026-06-07

### Added
- **同项目多实例（命名实例）**：`daemon start --name <inst>`（默认 `default`）可在同一项目下并行启动多个 Godot daemon；顶层 `--instance <name>` 对所有 RPC / run / daemon 子命令通用，daemon 子命令的 `--name` 是等价写法。
- **状态布局迁移 `.cli_control/instances/<name>/`**：daemon 的 pid / port / log 文件从项目根平铺改为按实例子目录存放。legacy 平铺路径（旧格式 `<hash>.json` 注册记录 + `.cli_control/port` 等）仅作只读 fallback 兼容，升级期间无需手工迁移；停掉旧 daemon 后文件自动收敛。硬编码读 `.cli_control/port` 的脚本应改读 `.cli_control/instances/default/port`，或改用 `GameClient()`/`GameBridge()` 自动发现（支持 `instance=` 参数）以免再次硬编码。
- **`daemon ls` 新增 instance 列**：JSON 每条 `result.daemons[]` 含 `"instance"` 字段；text 输出格式改为 `pid\tport\tinstance\tproject_root\tstarted_at`。
- **`daemon stop --all --project <path>`**：限定停某个项目下的全部实例（原 `--all` 不带 `--project` 仍停全局所有项目的所有实例）。
- **daemon 各子命令的 JSON envelope result 带 `"instance"` 字段**：`start` / `stop` / `status` / `logs` 均在 result 中增加 `"instance"` 键，便于 agent 确认操作目标。
- **`GameBridge(instance=...)` / `GameClient(instance=...)`**：`port=None` 时 `instance` 参数生效，自动查找对应命名实例的端口；显式 `port` 优先（向后兼容）。
- **pytest 多实例工厂 fixture `godot_instances`**（#143）：联机 e2e 在单测里同时拿 server / client bridge——`godot_instances.start("server")` 幂等启动命名实例并返回已连接 `GameBridge`，teardown 自动停掉本 fixture 起的全部实例（已在跑的只连不杀）；`stop(name)` 支持中途显式停（掉线场景）后重启，`daemon(name)` 暴露底层 `Daemon`。新增 pytest 选项 `--godot-cli-instances-scope=function|session`（默认 function 每用例隔离；session 整套共享省启动时间）。
- **`--instance all` 一条命令广播全部活实例**（#145）：任一 RPC 子命令对 cwd 项目全部活实例并发执行，聚合信封 `{"instances":[{instance, ok, result|error, rc}...], "rc": 0|3}`（entry 复刻单命令信封）；退出码全 0→0、任一非 0→3（沿 `daemon stop --all` 先例）。字符串参数中的 `{instance}` 逐实例替换；广播 `screenshot` 的路径缺 `{instance}` 时 preflight 报 -1003/64。`all` 成为保留实例名（`daemon start --name all` 拒绝）；`run`/`daemon` 子命令不支持广播。

### Changed
- **多实例选靶语义（处处一致）**：0 个在跑 → 选 default（legacy fallback）；1 个在跑 → 自动选中；≥2 个在跑且未指定 `--instance` / `--name` → `-1003` / exit 64，message 列出在跑实例名（`"multiple instances running: ... — pass --instance <name>"`）。显式 `--instance nope` 但该实例未在跑 → `-1003` / exit 64，message 附当前在跑实例列表。`--instance` 与 `--port` 互斥。
- **选中实例 live 但 port 文件尚不可读（daemon 启动中的瞬态）→ `-1003` / exit 64 并提示稍后重试**（#144）：此前静默回退默认端口、连接失败干等 30s，agent 拿不到「正在启动，请重试」的信号。

### Fixed
- **`daemon stop --all --project <path>` 对没有任何 daemon 在跑的项目不再伪造 `default` 条目**（#144）：text 输出 `(no running daemons)`、JSON `{"stopped": [], "rc": 0}`，与 `--all` 全局空注册表的形状一致。
- **旧版本 CLI 启动的录像 daemon 用新版 `daemon stop` 停止时录像正常转码**（#144）：此前 legacy 平铺路径的 `movie_path` 无人消费，录像静默残留；legacy `last_exit_code` 死文件也一并清理。
- **kill -9 留下的死实例目录 `.cli_control/instances/<name>/` 在状态文件清空后自动回收**（#144）：`godot.log` 等诊断文件在场时目录保留不动。

## [0.2.17] - 2026-06-06

### Fixed
- **#137 `screenshot --node` 在 hiDPI / viewport stretch 下裁剪错位**（PR #138）：`compute_node_screen_rect` 漏乘 viewport final transform——rect 算在画布（逻辑设计分辨率）坐标系，而取图侧 `get_image()` 是窗口物理像素系；平窗（scale=1）两系重合所以长期未暴露。Retina / `canvas_items` stretch 下裁出的区域偏移或截半。

## [0.2.16] - 2026-06-06

### Added
- **`find_godot_binary` PATH 候选名补 .NET/mono 版别名**（PR #134）：`godot4-mono` / `godot-mono` / `godot_mono` / `Godot_mono`，排标准名之后（双版并存优先轻量标准版）。macOS `/Applications` 与 Windows Program Files 的 glob 天然覆盖 mono 包名，零改动。
- **#135 macOS 检测兜底扫描 `~/Applications`**（PR #136）：`/Applications` 优先、用户级目录兜底，覆盖无管理员权限安装的场景。

## [0.2.15] - 2026-06-05

### Added
- **feat(wait): `wait-prop` / `wait-signal` / `wait-frames` 条件等待原语 + 错误码 1007（#96）**: 三个新 CLI 子命令。`wait-prop <path> <prop> <value> [--op eq|ne|gt|lt|ge|le] [--timeout] [--tolerance]` 等属性满足条件（exit 0=matched, 1=timeout）；`wait-signal <path> <signal> [--timeout]` 等信号发射（exit 0=emitted, 1=timeout）；`wait-frames <N> [--physics]` 推进 N 帧。服务端错误码 1007 SIGNAL_NOT_FOUND（wait-signal 传未知信号名，永久性 schema 错，不应 retry）。Python `GameClient` 对应方法：`wait_property` / `wait_signal` / `wait_frames`。
- **#110 `wait_signal` 超时与节点释放可区分**：未命中时返回体新增 `reason` 字段，值为 `"timeout"`（信号在超时前未发射）或 `"node_freed"`（等待期间目标节点被释放）。命中路径（`emitted: true`）不含 `reason`，对齐 `wait_property` matched 时无 reason 的约定。非 BREAKING（新增可选字段）。
- **feat: `get_properties` 多属性同帧原子读 + `get` 多属性形式（#100）**：`get <path> <prop1> <prop2>` 一次 RPC 同帧读取多个属性，不存在部分新鲜/部分旧数据的竞态；任一属性缺失整体失败（1002，message 点名所有缺失项）。sub-path 形式（`position:x`）在多属性列表中也支持。
- **feat(input): press/tap/hold/combo 注入 InputEventAction 进事件管线，_input/_unhandled_input 可见（#97）**：之前的实现只调 `Input.action_press/release`，仅翻轮询状态位，事件回调收不到。现改为 `Input.parse_input_event(InputEventAction)`，轮询（`is_action_pressed`/`get_vector`）与事件回调（`_input`/`_unhandled_input`）两种游戏写法均可感知注入输入。边界：`InputEventAction` 不携带鼠标坐标，依赖位置的 `_gui_input` 控件仍需用 `click`。
- **feat(scene): `scene-reload` / `scene-change` + pytest `fresh_scene` fixture + 错误码 1008（#98，PR #121）**：重载当前场景 / 切换到指定场景并阻塞到新场景 ready（per-test 隔离原语）；无 current scene、路径不存在、超时报 1008。`fresh_scene` fixture 让用例从干净场景开始；reload 后此前缓存的节点路径全部失效。
- **feat(time): `time-scale` / `pause` / `unpause` / `step-frames` + 错误码 1009（#102，PR #125）**：读写 `Engine.time_scale`（合法域 (0, 100]，`wait-time` 按 game time 计、倍速后语义不变）、暂停/恢复 SceneTree、paused 下确定性推进 N 帧（物理断言原语）。未 pause 调 `step-frames` 报 1009。
- **feat(render): `sprite-info` + `screenshot --node` + 错误码 1010/1011（#101，PR #130）**：`sprite-info` 一次拿齐 Sprite2D / AnimatedSprite2D / TextureRect 的 texture、实际图集区域（effective_region / frame_texture）、翻转、帧号、modulate、visible（纯属性读，headless 可用）；`screenshot --node` 按节点屏幕 AABB 裁剪小图供像素级断言。非 sprite 类 → 1010，屏幕外/零尺寸 → 1011。
- **feat(diag): `errors` 结构化查询 + `daemon logs --tail` + pytest 失败配套 + 错误码 1012（#103，PR #131)**：Logger 拦截 push_error / push_warning，seq 游标增量查询（`--since` 实现「本用例期间零错误」断言；需 Godot 4.5+，老引擎报 1012）；`daemon logs --tail N` 直接读 `.cli_control/godot.log`（daemon 已退出也可用，post-mortem 排查）；pytest 新增 `no_push_errors` fixture（用例期间出现新 push_error 即失败）与失败自动截图（非 headless 时存 `.cli_control/failures/<nodeid>.png`）。
- **feat(init): 默认覆盖 addon 目录 + `--keep-addon` 逃生口（PR #133）**：重跑 `init` 即同步 `addons/godot_cli_control/` 与 SKILL.md 到当前 CLI 版本（插件目录 wipe 后重拷，`project.godot` patch 保持幂等）。`--force` 降级为兼容 no-op（与 `--keep-addon` 互斥）。

### Changed
- **BREAKING** `get` 复合 Variant 返回从旧版 `"(x, y)"` 字符串改为 set-schema 数组 + type 字段；信封 result 从裸值变 `{"value": <array-or-scalar>, "type"?: "<GodotType>"}` 对象（#99）。写侧 `set` 接受的 Array layout 完全一致，get→set round-trip 无需转换。Python API `get_property` / `get_properties` 只返回裸 value（type 已剥离），要拿 type 字段走 CLI `get` 或底层 `client.request("get_property", ...)`。
- **pytest `bridge` fixture teardown 兜底还原全局态（#124，PR #126）**：unpause + 把 `time_scale` 还原到 setup 时快照值（非盲写 1.0，不打断 `--godot-cli-time-scale` 整套加速）+ `release_all()`。上个用例崩溃残留的 pause / 倍速不再污染下一个。

### Fixed (BREAKING — exit code changes)
- **#111 argparse 用法错 exit 2 → exit 64**: 所有 argparse 层错误（缺位置参数、非法 choices、未知子命令等）现在统一输出 `-1003` 信封并以 exit 64 退出。之前是 exit 2（argparse 默认）。影响：脚本若用 `[ $? -eq 2 ]` 判 argparse 错需改为 `[ $? -eq 64 ]`。
- **#92 `run <script>` 前置错误拆分**:
  - 脚本路径不存在 / 脚本缺 `run(bridge)` 函数：由旧的 exit 2（或 exit 1）改为 exit 64，错误码由 `-1003` 保持不变。这是用法错，应修正调用而非重试。
  - `daemon start`（`run` 自动启停 / `daemon start` 命令）/ `daemon stop` 系统级失败（端口冲突、找不到 Godot binary、PID 文件丢失等）：错误码由 `-1003` 改为 **新码 `-1006` (PRECONDITION)**，exit code 保持 exit 2。影响：`run`/`daemon` 失败时，通过 `error.code` 区分用法错（`-1003` → 修调用）与基础设施错（`-1006` → 修环境）现在变得无歧义。

### Performance
- **#109 `wait_property` 免物化直比 + 属性存在性 memo（PR #132）**：逐帧轮询不再每帧物化编码整个属性值；顺手修了 GDScript 跨类型 `==` 是运行期脚本错误（中止函数抛 Nil、非静默 false）的存量雷（`_safe_eq` 守卫）。

## [0.2.14] - 2026-06-03

仅打包元数据 / 文档：PyPI keywords / classifiers / urls、社区健康文件（CONTRIBUTING / SECURITY / issue & PR 模板）、修复失效的 git 安装命令。无代码行为变更。

## [0.2.13] - 2026-06-02

### Added
- **examples/platformer-demo 可跑示例（#89）**：自带 Start 按钮 + 跳跃角色场景与 `drive.sh` 全链路演示，CI 下真 Godot 驱动防腐。

### Fixed
- **批量修复 10 个 issue**：CLI 子命令端口从 `.cli_control/port` 自动发现、`-1003` 用法错退出码统一为 64（#82）、`tap`/`hold` 加 `--wait` 阻塞选项、ruff 进 CI 门禁等。

## [0.2.12] - 2026-06-01

### Added
- **#80 `init` 把 `.cli_control/` 写进目标项目 `.gitignore`**，避免提交机器本地状态（port / pid / log）。

## [0.2.11] - 2026-05-31

### Fixed
- **#79 拒绝 `--record` + `--headless` 组合**：Godot Movie Maker 在 headless 下 SIGSEGV（上游问题），preflight 直接报用法错（下游 CultivationWorld #180 根因）。

## [0.2.10] - 2026-05-24

### Fixed
- Windows / 测试修补批次：`run_gut.py` 强制 UTF-8 stdout/stderr（#36 Windows GUT 中文崩溃）、Windows 注册表目录（#43）、`run` 收尾 SIGTERM 噪音消除（#67）、`_build_tree` LIMIT 常量消歧（#48）。

## [0.2.9] - 2026-05-23

### Fixed
- **输入持续性**：断连按 WebSocket close code 区分「清 / 不清」持有中的输入；`hold` duration 加校验。崩断的客户端不再把按住的方向键永久遗留在游戏里。

## [0.2.8] - 2026-05-16

### Added
- **`GODOT_CLI_LONG_OP_TIMEOUT=<seconds>`** 环境变量抬高 600s 客户端长操作上限（超 10 分钟的录制）；`wait_game_time` / `combo` 去掉 wall-clock 上限，靠 ws 心跳兜底（#45）。
- **`run` 静态检测脚本里的 `screenshot` 自动 force GUI（#65）**：headless 截图必失败，提前规避。

## [0.2.7] - 2026-05-16

仅文档：SKILL.md / `-h` / 旁路文档以代码为标准对齐（11 处缺漏与不准确）。无代码行为变更。

## [0.2.6] - 2026-05-16

### Fixed
- **#61 `screenshot` 首帧 transient 失败根因消除 + 兜底**：viewport texture 未就绪时不再偶发 1006。

## [0.2.5] - 2026-05-16

### Fixed
- **#52 `set` 走 JSON Array 喂 Vector/Color/Rect 时静默失败**：`set zoom '[1.8, 1.8]'` 等价于 `node.set("zoom", [1.8, 1.8])`，Godot 隐式构造失败 → 实际值是 `Vector2(0,0)` 或被 clamp 到 `0.00001`，但服务端仍返 `{success: true}`。`handle_set_property` 现在查 `get_property_list()` 拿声明类型，把 numeric Array 转成对应 Variant。长度不匹配或元素非数字时 fail-loud 返 `-32602 "value type mismatch ..."`，不再 silent corruption。
- **sub-path 标量赋值现在真的会写入**：之前 `set <node> position:x 1.8` 调的是 `Object.set("position:x", 1.8)`，Godot 4 的 `Object.set` 把整串当字面属性名找不到就 silent no-op（依旧返 `{success: true}` 但 `position.x` 不变）。改用 `Object.set_indexed(NodePath, value)` 才会按 sub-path 写入。是 #54 review 阶段被新加的 `test_set_subpath_scalar_still_works` 捕获的隐藏 silent-fail。
- **CLI flag position**: `--json` / `--text` / `--no-json` 现在在 RPC 子命令尾部也接受（之前只能写最前面）。argparse 子 parser 用 `default=argparse.SUPPRESS` + 顶层 `set_defaults` 兜底，避免子 parser 默认值覆盖父 parser 解析结果。
- **Error code 1004 collision**: `low_level_api.gd` 的 scene tree 超限改用新业务码 `1005 "scene tree too large"`，与 input_simulation `1004 "combo in progress"` 解耦。新增 `error_codes.gd` 集中常量。
- **pytest fixture default port**: `--godot-cli-port` 默认从 9877 改为 0（OS-assigned），与 `daemon start` 默认对齐；多项目并行测试不再撞端口。
- **Addon README error-code table**: 之前只列到 1003，补全 1004 / 1005 / 客户端 -1xxx 段。
- **Scene tree hard limit bypass (DoS fix)**: `handle_get_scene_tree` 入口现在把 `max_nodes` clamp 到硬墙 `_BUILD_TREE_MAX_NODES` (5000)。修复前客户端传 `max_nodes=999999` 会让服务端先把整棵超大树构造成 Dictionary 再被 1005 错误丢弃（内存浪费 / OOM 路径）。
- **Error code 1003 semantic split**: screenshot 在 viewport texture 为 null 时不再借用 `1003 METHOD_NOT_FOUND`，改用新业务码 `1006 RESOURCE_UNAVAILABLE`。1003 现在是纯 schema 错（不应 retry），1006 是 transient 错（短重试可能成功），agent 据此分别处置。
- **`cmd_run` / `cmd_init` 真正输出 JSON envelope（#50）**；`GameBridge` 补 `get_scene_tree` / `wait_game_time` / `action_tap` alias 兑现「方法名一致」承诺（#58 #60）。

### Added
- **#54 全部复合 Variant 都支持 Array → Variant coerce**：从 4-float 简单类型到 16-float 矩阵，全部按 axis-vector 顺序的 flat numeric Array 写入（每 N 元素 = 一个 Vector 轴）。覆盖：`Vector2/2i/3/3i/4/4i`、`Rect2/2i`、`Color`（3-element=RGB / 4-element=RGBA）、`Plane(a,b,c,d)`、`Quaternion(x,y,z,w)`、`AABB(pos 3, size 3)`、`Basis(9 axis-vector)`、`Transform2D(xaxis 2, yaxis 2, origin 2)`、`Transform3D(basis 9, origin 3)`、`Projection(16 axis-vector)`。具体 layout 见 SKILL.md。`Plane.normal` 和 `Quaternion` 不会自动归一化（与 Godot ctor 一致）。
- **#54 防御性 fallback**：未在 coerce 名单也不在 `_ARRAY_PASSTHROUGH_SAFE_TYPES`（基本类型 / Object / 集合 / Packed*Array）白名单的声明类型 + Array 输入会 fail-loud，避免未来 Godot 加新 compound Variant 时 silent-corrupt 回归。
- **#54 sub-path + Array 现在 fail-loud**：`set <node> transform:origin '[10, 20, 30]'` 在 Godot 里会 silent-corrupt（Vector3 leaf 不会从 Array 隐式构造，origin 仍是 (0,0,0)），server 现在主动返 `-32602 "sub-path + Array is not supported"` 提示走 top-level 形式（如 `set <node> transform '[basis 9, origin 3]'`）。sub-path 标量赋值（`position:x 1.8`）不变。
- `tree --max-nodes <N>`（默认 200）：节点数软上限；超出时响应含 `truncated: true` + `total_nodes`，agent 据此决定分子树。硬墙仍是 5000 节点 → `1005`。
- `set` / `call --text-value`：禁用 JSON 解析、把 value/args 强制按字符串处理，避开 `null` / `true` / `42` 这类字面量被解析成 Variant 类型的 footgun。

### Changed
- **BREAKING (轻微)**：`daemon start` / `run` 默认 headless 行为改为基于 `sys.stdout.isatty()` 自动判定 —— pipe / CI / agent shell 默认 headless；交互终端默认开窗。新增 `--gui` 强制开窗 flag。`--headless` 仍可显式传，覆盖自动判。脚本里依赖 "默认会开窗" 的需要加 `--gui`。

## [0.2.4] - 2026-05-01

### Changed
- **BREAKING (轻微)：默认 daemon 端口从 9877 改为 OS-assigned**。实际端口写入 `.cli_control/port`，CLI 子命令与 `GameClient()`（不传 port）自动发现。硬编码 `127.0.0.1:9877` 的外部脚本要么读 `.cli_control/port`，要么显式传 `--port 9877`。

### Added
- **`daemon ls`**：跨项目列出运行中的 daemon（全局注册表 + 死记录自动清理）。
- **`daemon stop --all` / `--project <path>`**：批量 / 跨项目停止。
- **`daemon start --idle-timeout 30m`**：opt-in 空闲自动 shutdown；项目级默认可写 `.cli_control/config.json`（`{"idle_timeout": "30m"}`），显式 flag 优先。
- `daemon start` 前先 bind 校验端口空闲，被占立即报错。

## [0.2.3] - 2026-05-01

### Added
- **`GODOT_CLI_CONTROL=0` 强制禁用逃生口**：env var 一票否决，覆盖其它激活方式。

## [0.2.2] - 2026-04-29

### Security / Hardening
- **封堵 `set_property` 黑名单 NodePath sub-property 旁路**（`script:source_code` 类路径绕过黑名单）。
- handler 加固：async type-check、action 名校验、scene-tree 节点数上限。
- Python 测试覆盖率接入 80% 门禁。

## [0.2.1] - 2026-04-29

与 v0.2.0 同代码（发布管线重跑），无变更。

## [0.2.0] - 2026-04-29

### AI-friendly CLI 改造（多个 BREAKING change）

把 CLI 重定位成 AI agent 的一等接口：默认结构化输出、补齐读 / 写 / 发现的 shell 命令、明确退出码契约。shell-only 的 agent 现在不需要写 Python 脚本就能完成全部操作。

#### Added
- **12 个新 RPC 子命令**（覆盖客户端全部能力）：`get` / `set` / `call` / `text` / `exists` / `visible` / `children` / `wait-node` / `wait-time` / `pressed` / `combo-cancel` / `actions`。
- **顶层 `--json` / `--text` / `--no-json`**：默认 `--json`，输出统一信封 `{"ok": true, "result": ...}` 或 `{"ok": false, "error": {"code": N, "message": "..."}}`，单行 stdout 易于 jq / json.loads。
- **`combo` 三种喂法**：`--steps-json '[...]'`（inline）/ `combo combo.json`（文件，旧用法保留）/ `combo -`（stdin）。
- **`exists` / `visible` / `wait-node` exit-code-as-result**：shell `if godot-cli-control exists /root/Foo; then …` 直接可用。
- **`actions` 默认过滤 `ui_*` 内置**，`--all` 看全。
- **`list_input_actions` RPC**（GD 端）+ `GameClient.list_input_actions(include_builtin)` + `GameClient.get_pressed()`：客户端缺的两个方法补齐。
- **`RpcError` 异常**（继承 `RuntimeError`）保留服务端 `code` 字段，给上层做特定错误码 retry / 信封序列化。
- **GUT 测试**覆盖 `list_input_actions` 默认过滤 / `--all` / 字母序。

#### Changed
- **BREAKING**：`--json` 默认开。RPC 子命令的 stdout 现在是 JSON 信封；旧的人类可读字符串通过 `--text`（或别名 `--no-json`）保留。
- **BREAKING**：`screenshot` 的 `output_path` 改成必填。旧"省略路径 → base64 灌 stdout"行为已删（撑爆 LLM 上下文）。
- **BREAKING**：RPC 错误从裸 traceback 改成结构化信封，exit code 1（RPC error）/ 2（连接、超时、用法错误）。脚本里靠 grep traceback 文本判错的需要切到读 exit code + JSON `error.code`。
- `daemon status` 在 JSON 模式下也产出信封 `{"state": "running", "pid": ..., "port": ...}` 或 `{"state": "stopped"}`；exit code 语义不变（0=running, 1=stopped）。
- SKILL.md 模板重写：新增 *AI Quickstart* / *Exit codes* / *JSON envelope* 段；shell vs Python 论调倒过来——shell 是 canonical，Python 桥仅在跨步保持单连接时使用。

#### Fixed
- `GameClient.request()` 之前丢掉了服务端 `error.code`（统一 raise `RuntimeError` 只带 message）；现在 raise `RpcError(code, message)` 透传。
- `combo` 用法错误（无 steps、`--steps-json` 与位置参数互冲）现在通过 preflight 在连 daemon **之前**报 `EXIT_USAGE(64)`，不再让 agent 干等 30s 连接 retry。
- connection retry 日志从 `WARNING` 降到 `DEBUG`：`cmd 2>&1 | jq` 不再被 retry 行污染；最终的 `ConnectionError` 仍由 dispatcher 统一信封到 stdout。
- `daemon start` 在 `--json` 模式下产出 `{"ok":true,"result":{"started":true,"port":N,"pid":N}}`；`daemon stop` 输出 `{"ok":true,"result":{"stopped":true,"rc":N}}`，`rc` 透出方便 agent 判定 ffmpeg 转码是否成功。Text 模式行为不变。

#### Docs
- SKILL.md 增加 *Error code reference*：服务端 `1001-1004` / JSON-RPC `-32xxx` / 客户端 `-1xxx` 三段含义的总表（含新增的 `-1004` IO error 与 `-1099` internal error）。
- SKILL.md 增加 `set` / `call` 的安全 blacklist 提示（`queue_free` / `set_script` 等会被 `-32602` 拒绝），以及 JSON 解析 footgun 例（`set ... text null` 会存 Variant null，要存字符串得 `'"null"'`）。
- README.md *Highlights* 重写"Two client APIs"行为"Shell is canonical"，反映新的 CLI 一等地位。
- `GameBridge` (sync) 补齐 `get_text` / `is_visible` / `combo_cancel` 三个方法，与 async `GameClient` 表面对齐。

#### Hardening (post-review)
- `_run_rpc` 加兜底 `except Exception` 把任何未预见的客户端异常（`AttributeError` / `KeyError` / 协议解析意外等）也落进 `-1099` 信封，绝不让 traceback 漏到 stdout 破坏 `--json` 契约。完整 traceback 仍写 stderr 帮 debug。
- `OSError` 拆分：网络层（`socket.gaierror` / errno ECONNREFUSED 等）继续走 `-1001`；本地文件 IO（`screenshot` 写盘失败常见的 `PermissionError` / `FileNotFoundError`）走新增的 `-1004` (IO)，避免 agent 误以为 daemon 挂了去重启。
- `combo -` 检测 stdin 是 TTY 时直接拒绝（之前会 `read()` 无限阻塞）。
- `screenshot` 路径自动 `expanduser()`：`screenshot ~/foo.png` 不再创建字面 `~` 目录。

#### Out of scope (intentional)
- `run <script.py>` 子命令保留旧的人类可读 stderr 输出。它是交互式脚本宿主，不是 RPC，新的 JSON 信封契约只覆盖 RPC 子命令 + daemon 三命令。

## [0.1.7] – [0.1.10] - 2026-04-29

发布管线 / 打包修复批次：`pyproject.toml` 提升到仓库根（0.1.7）、`init` 重新导入项目以刷新 `global_script_class_cache`（0.1.9）。

### Changed
- **`auto_enable_in_debug` 默认改为 true**（0.1.10）：编辑器 F5 直接生效，不再需要手动激活（release build 仍无条件禁用）。

## [0.1.6] - 2026-04-29

### Added
- `godot-cli-control init`: 一键 onboarding 子命令 + 跨平台 Python daemon (取代手写 wrapper)。
- pytest11 entry-point: `godot_daemon` / `bridge` fixtures,装包即用。
- PowerShell wrapper (`bin/run_cli_control.ps1`),原生 Windows 不需 WSL。
- LowLevelApi: 项目可通过 ProjectSetting 扩展 property/method 黑名单。
- GameBridge: `outbound_buffer_size` 可经 ProjectSetting 配置。
- Skill 集成:`init` 默认渲染并写入 Claude + Codex 的 `SKILL.md`(`--no-skills` / `--skills-only` / `--skills-no-clobber` 三档可选)。
- CLI `-h`:子命令分组 + per-command usage / examples。
- GUT 单测覆盖 `LowLevelApi` + `InputSimulationApi`,接入 CI。
- CI 矩阵扩展到 Windows + macOS;walkthrough 改写为 Python。

### Changed
- **BREAKING**: 移除基于方向的 `move_*` API — 插件不应假设项目里的 action 名(811ce99)。
- `runner` 委托到 `cli._exec_user_script`,统一脚本执行路径。
- `cli.build_parser` 改为 public(为 skill 渲染暴露)。
- `__version__` 改为从 `_version.py`(hatch-vcs 生成)读取,不再硬编码。
- 安装文档统一切到 `pipx install godot-cli-control`。

### Fixed
- `bridge`: 加固 JSON-RPC,容忍畸形请求与超长 combo steps。
- `bridge`: 日志里的 URL 与实际 listen addr 对齐。
- `daemon`: 启动时清理上一次残留的 `movie_path`。
- `client`: 使用 `127.0.0.1` 字面量连接,绕开 `localhost` IPv6 解析问题。
- `cli`: 用户脚本可被 import + dataclass 友好;`finally` 块恢复 `sys.path` / `sys.modules`。
- `cancel_combo` 正确把响应回送给 caller;反射类方法纳入黑名单。
- 截屏在 headless 模式下不再 hang。
- CI(Windows runner):强制 stdout utf-8 让中文 `print` 不崩;保留 Godot 原始 exe 名。

## [0.1.0] – [0.1.5]
- 早期未维护 changelog;关键节点见 git tag `v0.1.0`..`v0.1.5`。
- Initial release as Godot 4 plugin + Python package.
- 21 RPC methods (节点查询 / 操作 / 输入模拟 / 截图 / 等待).
- Three activation modes: `--cli-control` flag, `GODOT_CLI_CONTROL=1` env var, Project Setting (debug-build only).
- Property/method blacklist for security.
- Movie writer / record support via wrapper script.

### Known Limitations
- Headless + Movie Maker is incompatible (Godot upstream issue): record requires GUI mode.
- Default `GODOT_BIN` path is macOS-specific.
- Windows 用户从 0.1.5 起可用 PowerShell wrapper / Python daemon,无需 WSL。
