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

const INVALID_PARAMS: int = -32602
const INVALID_REQUEST: int = -32600
const METHOD_UNKNOWN: int = -32601
