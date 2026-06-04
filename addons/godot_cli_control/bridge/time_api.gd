class_name TimeApi
extends Node
## 时间控制 API：time_scale / pause / unpause / step_frames（issue #102）
##
## 挂在 GameBridge（PROCESS_MODE_ALWAYS）下，tree paused 时 RPC 照常工作——
## 这是 step_frames「paused 下推帧」的机制基础。错误码常量见 error_codes.gd。

# time_scale 合法域 (0, 100]：=0 冻死 wait-time 且与 pause 职责重叠，拒收；
# 上限防呆（误传 1e9 之类）。
const _MAX_TIME_SCALE: float = 100.0
# step_frames 防呆上限，对齐 wait_api._MAX_WAIT_FRAMES
const _MAX_STEP_FRAMES: int = 3600


## 解析 --cli-time-scale=<x> 启动参数（解析结构同 _parse_port_from_args，
## 但非法值用 printerr 而非 push_warning——headless 下 stderr 是用户唯一可见渠道）。
## 无该参数返回 -1.0；非法值（非数字 / 越界）printerr 警告后也返回 -1.0
## ——不挡启动。静态纯查询：GameBridge 传 OS.get_cmdline_args()，GUT 传构造数组。
static func parse_cmdline_time_scale(args: PackedStringArray) -> float:
	for arg: String in args:
		if arg.begins_with("--cli-time-scale="):
			var parts: PackedStringArray = arg.split("=", false, 1)
			if parts.size() != 2 or not parts[1].is_valid_float():
				printerr("GameBridge: invalid %s ignored (must be a number in (0, %s])" % [arg, _MAX_TIME_SCALE])
				return -1.0
			var scale: float = parts[1].to_float()
			if scale <= 0.0 or scale > _MAX_TIME_SCALE:
				printerr("GameBridge: invalid %s ignored (must be in (0, %s])" % [arg, _MAX_TIME_SCALE])
				return -1.0
			return scale
	return -1.0


## value 缺省 = 读当前值（Engine 不是节点，get 够不着，这是唯一读通道）。
func handle_time_scale(params: Dictionary) -> Dictionary:
	if not params.has("value"):
		return {"time_scale": Engine.time_scale}
	var raw: Variant = params["value"]
	if not (raw is int or raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'value' must be a number")
	var scale: float = float(raw)
	if scale <= 0.0 or scale > _MAX_TIME_SCALE:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"value must be > 0 and <= %s (use pause to freeze the game, not time_scale 0)" % _MAX_TIME_SCALE,
		)
	Engine.time_scale = scale
	return {"time_scale": scale}


func handle_pause(_params: Dictionary) -> Dictionary:
	get_tree().paused = true
	return {"paused": true}


func handle_unpause(_params: Dictionary) -> Dictionary:
	get_tree().paused = false
	return {"paused": false}


## paused 前置下确定性推进 N 帧再停。未 paused → 1009（fail-loud：
## 教会 agent「pause → step → 断言」的正确模型，不隐式改全局状态）。
func step_frames_async(params: Dictionary) -> Dictionary:
	var frames_raw: Variant = params.get("frames", null)
	if not (frames_raw is int or frames_raw is float):
		return _err(CliControlErrorCodes.INVALID_PARAMS, "'frames' must be an integer")
	var frames: int = int(frames_raw)
	if frames < 1 or frames > _MAX_STEP_FRAMES:
		return _err(
			CliControlErrorCodes.INVALID_PARAMS,
			"frames must be 1..%d (got %d)" % [_MAX_STEP_FRAMES, frames],
		)
	var physics: bool = bool(params.get("physics", false))
	if not get_tree().paused:
		return _err(
			CliControlErrorCodes.NOT_PAUSED,
			"step_frames requires the tree to be paused — call pause first",
		)
	get_tree().paused = false
	# 并发边界：process_frame/physics_frame 是 SceneTree 信号，paused 下也照常
	# 发射（pause 只停节点回调，不停主循环），所以即使推进期间有并发 pause RPC
	# 把树暂停，本循环也不会挂死——只是被暂停的那些帧不推进游戏逻辑（帧数照计）。
	for _i in frames:
		if physics:
			await get_tree().physics_frame
		else:
			await get_tree().process_frame
	# 无条件恒写：即使推进期间外力改了 paused，返回时也保证停在 paused
	get_tree().paused = true
	return {"stepped": frames, "paused": true}


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
