extends Logger
## push_error / push_warning 拦截器（issue #103）。
##
## 单独成文件且**不带 class_name**：Logger 是 Godot 4.5+ API，本文件只能
## 由 diagnostics_api.gd 在 ClassDB.class_exists("Logger") 通过后 load()。
## 任何对本文件的静态 preload / class_name 全局注册都会让老引擎在编译
## 阶段就炸掉整个 addon —— 动态 load 把兼容面隔离在这一个文件里。
##
## 实测形态（Godot 4.6.2）：push_error("msg") 到达时 code="msg"、
## rationale=""、function="push_error"、file 是 C++ 源文件；真正有用的
## GDScript 调用位置在 script_backtraces 的首帧（release 构建下可能为空）。

## 由 diagnostics_api 注入：func(entry: Dictionary) -> void。
## _log_error 可能从任意线程进来，sink 的实现负责自己的线程安全。
var sink: Callable = Callable()

const _TYPE_NAMES: Dictionary = {
	ERROR_TYPE_ERROR: "error",
	ERROR_TYPE_WARNING: "warning",
	ERROR_TYPE_SCRIPT: "script",
	ERROR_TYPE_SHADER: "shader",
}


func _log_error(
	function: String,
	file: String,
	line: int,
	code: String,
	rationale: String,
	_editor_notify: bool,
	error_type: int,
	script_backtraces: Array[ScriptBacktrace],
) -> void:
	if not sink.is_valid():
		return
	# 引擎内部错误 message 在 rationale、push_error 的在 code —— 取非空者
	var message: String = rationale if not rationale.is_empty() else code
	var source: Variant = null
	for bt: ScriptBacktrace in script_backtraces:
		if bt != null and bt.get_frame_count() > 0:
			source = "%s:%d @ %s" % [
				bt.get_frame_file(0), bt.get_frame_line(0), bt.get_frame_function(0),
			]
			break
	sink.call({
		"type": _TYPE_NAMES.get(error_type, "error"),
		"message": message,
		"function": function,
		"file": file,
		"line": line,
		"source": source,
	})


func _log_message(_message: String, _error: bool) -> void:
	pass  # 只收 error/warning；普通 print 进 ring 会把 buffer 冲成噪音
