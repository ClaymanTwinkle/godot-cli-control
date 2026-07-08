class_name CliControlErrorCodes
extends RefCounted
## 集中错误码常量。新加业务码必须在这里登记，
## 避免 1004 那种隐式撞码（input_sim 用 "combo in progress"，
## low_level 又用 "scene tree too large"）。
##
## 三段制（详见 SKILL.md 错误码表）：
##   1xxx        服务端业务码
##   -32xxx      JSON-RPC 标准
##   -1xxx       客户端（Python）侧；GDScript 这边不会产出

const NODE_NOT_FOUND: int = 1001
const PROPERTY_NOT_FOUND: int = 1002       # 也用于 "node has no 'text' property"
const METHOD_NOT_FOUND: int = 1003
const COMBO_IN_PROGRESS: int = 1004
const SCENE_TREE_TOO_LARGE: int = 1005
# 资源 transient 不可用（screenshot viewport texture null 等）。
# 与 1003 拆开：1003 是 schema 错（永久），1006 是时机错（短重试可能成功）。
# issue #61 落地后语义：GameBridge 启动 gate（H）保证 client 连上时 viewport
# 至少画过一帧 + take_screenshot_async 内部循环（D）兜底动态 transient，
# 所以正常用法下 1006 不应触发。但它仍是 last-resort 合法信号 ——
# client 必须保留对 1006 的处理（不要假设它消失），未来若改为 fail-loud
# 会让 scene 切换瞬间的截图变成硬错。
const RESOURCE_UNAVAILABLE: int = 1006
# 信号不存在（wait_signal 的 schema 错，永久性——与 1003 method、1002 property 同族）
const SIGNAL_NOT_FOUND: int = 1007
# 场景不可用（issue #98 scene_reload/scene_change）：reload 时无 current
# scene、change 的 res 路径不存在/加载失败、等新场景 ready 超时。
# 与 1006 拆开：1006 是「短重试可能成功」的 transient；1008 三种情形里
# 路径不存在是永久错，超时大概率是场景加载本身坏了——agent 应停下排查。
const SCENE_UNAVAILABLE: int = 1008
# step_frames 的状态前置错（issue #102）：tree 未 paused 时调 step_frames。
# 与 -32602 区分：参数本身没问题，是世界状态不满足前置——agent 应先 pause。
const NOT_PAUSED: int = 1009
# 节点类型不支持该可视化操作（issue #101）：sprite_info 打在非 sprite 类节点、
# screenshot --node 算不出节点边界（非 CanvasItem / 无法确定 local rect）。
# schema 类永久错（与 1002/1003/1007 同族）——agent 应换节点或换操作，重试无意义。
const UNSUPPORTED_NODE_TYPE: int = 1010
# screenshot --node 的裁剪框与视口交集为空（issue #101）：节点在屏幕外或
# 变换后尺寸为零。状态类错（与 1009 同族）——参数没问题，是世界状态不满足；
# agent 应先把节点挪进视口（移动 camera / 改 position / 等动画到位）再截。
const NODE_NOT_ON_SCREEN: int = 1011
# 引擎能力缺失（issue #103）：errors 捕获需要 Godot 4.5+ 的 Logger API，
# 老引擎上该 RPC 永久不可用。与 1006 区分：不是 transient，升级引擎前
# 重试无意义；与 1010 区分：错的不是目标节点，是宿主引擎版本。
const FEATURE_UNAVAILABLE: int = 1012
# screenshot 服务端落盘失败（issue #149）：path 打不开（父目录不存在 /
# 无写权限 / 路径非法）。与客户端 -1004 区分：那是 CLI 进程本地写不进，
# 这是 daemon 进程写不进。永久错（与 1002/1003 同族）——修路径前重试无意义。
const WRITE_FAILED: int = 1013
# drag 互斥（issue #154 P2）：已有一个 drag 协程在插值中又收到 drag 请求。
# 状态类错（与 1004 COMBO_IN_PROGRESS 同族）——同一时刻只允许一个鼠标拖拽
# 在途，agent 应等上一个完成（或 release-all 取消）再发。
const DRAG_IN_PROGRESS: int = 1014
# emit-signal 逃生门未开（issue #157 item4）：daemon 未带 --allow-emit-signal 启动时调
# emit-signal 子命令。前置条件错（与 1009 NOT_PAUSED 同族）——agent 应重启 daemon 加该
# flag（debug-build + localhost 之上第三重显式门）。emit_signal 默认仍在方法黑名单里，
# call <node> emit_signal 始终被拒。
const EMIT_SIGNAL_DISABLED: int = 1015
# 响应超出站 WebSocket 缓冲（issue #160）：单条响应 JSON 超过 outbound_buffer_size
# （默认 10MB，godot_cli_control/outbound_buffer_mb 可调）时 send_text 失败。
# 容量/资源类永久错——同一响应重试必再超；agent 应改用 path 落盘（screenshot）
# 或调大 buffer。daemon 用它替换发不出去的大响应，避免 client 干等到 -1002 假超时。
const RESPONSE_TOO_LARGE: int = 1016
# click 过滤器定位命中多于一个节点（find+click 原子化）：BFS 序对 agent 不可
# 预期，静默点第一个可能点错按钮（比报错贵得多）——宁可 fail-loud 列出候选
# 让 agent 收窄。状态类错（与 1004/1014 同族）：过滤器本身合法，是场景里
# 恰好有多个匹配。
const AMBIGUOUS_MATCH: int = 1017

const INVALID_PARAMS: int = -32602
const INVALID_REQUEST: int = -32600
const METHOD_UNKNOWN: int = -32601


## 错误码 → 「下一步怎么办」提示。空串 = 无提示（该码的 message 已足够具体，
## 如 -32602 每个 case 有专属文案）。唯一注入点是 GameBridge._send_error：
## 响应 error 对象带上可选 "hint" 字段，CLI 信封原样透传给 agent——让 agent
## 在错误发生点当场拿到指引，不依赖它读过 SKILL.md 错误码表。
## 客户端 -1xxx 的提示在 Python 侧 cli.py 的 _CLIENT_HINTS，两段各管各的
## （与错误码三段制同构）。hint 只补「下一步动作」，不复述错误本身。
static func hint_for(code: int) -> String:
	match code:
		NODE_NOT_FOUND:
			return (
				"path must start with /root; locate by text/type with `find`,"
				+ " or `wait-node <path>` if it may not be loaded yet"
			)
		PROPERTY_NOT_FOUND:
			return "inspect the node's properties with `tree <path> 1`"
		METHOD_NOT_FOUND:
			return (
				"inspect node methods with `tree`; for input actions run"
				+ " `actions` (or `actions --all`)"
			)
		COMBO_IN_PROGRESS:
			return "run `combo-cancel` (or `release-all`), then retry"
		SCENE_TREE_TOO_LARGE:
			return (
				"pass --max-nodes N, or query a subtree:"
				+ " `tree <path>` / `children <path>`"
			)
		RESOURCE_UNAVAILABLE:
			return (
				"usually transient - retry after `wait-time 0.05`; on a"
				+ " headless daemon screenshots never work (restart with --gui)"
			)
		SIGNAL_NOT_FOUND:
			return "list the node's signals with `tree`; check the spelling"
		SCENE_UNAVAILABLE:
			return (
				"check the res:// path; if loading hangs, inspect"
				+ " `daemon logs --tail 50`"
			)
		NOT_PAUSED:
			return "call `pause` first, then `step-frames`"
		UNSUPPORTED_NODE_TYPE:
			return (
				"aim at a drawable node (often the child sprite),"
				+ " or use another command"
			)
		NODE_NOT_ON_SCREEN:
			return "move the camera/node into view (or wait for it), then retry"
		FEATURE_UNAVAILABLE:
			return (
				"requires a newer Godot (errors capture needs 4.5+);"
				+ " upgrade the engine or drop this call"
			)
		WRITE_FAILED:
			return "fix the destination path/permissions (daemon-side), then retry"
		DRAG_IN_PROGRESS:
			return (
				"wait for the in-flight drag to finish,"
				+ " or `release-all` to cancel it"
			)
		EMIT_SIGNAL_DISABLED:
			return "restart the daemon with --allow-emit-signal"
		RESPONSE_TOO_LARGE:
			return (
				"pass a file path so the daemon writes to disk,"
				+ " or raise godot_cli_control/outbound_buffer_mb"
			)
		AMBIGUOUS_MATCH:
			return (
				"narrow the filters (--exact / --type / --from / --name-pattern)"
				+ " until exactly one node matches, or run `find` and click"
				+ " the path directly"
			)
		METHOD_UNKNOWN:
			return (
				"client and addon versions drifted - re-run"
				+ " `godot-cli-control init` to sync the addon"
			)
		_:
			return ""
