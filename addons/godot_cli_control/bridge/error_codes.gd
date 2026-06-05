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

const INVALID_PARAMS: int = -32602
const INVALID_REQUEST: int = -32600
const METHOD_UNKNOWN: int = -32601
