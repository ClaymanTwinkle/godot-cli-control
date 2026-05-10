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
const METHOD_NOT_FOUND: int = 1003         # 也用于 "screenshot unavailable"
const COMBO_IN_PROGRESS: int = 1004
const SCENE_TREE_TOO_LARGE: int = 1005

const INVALID_PARAMS: int = -32602
const INVALID_REQUEST: int = -32600
const METHOD_UNKNOWN: int = -32601
