## GUT 单元测试：RenderApi sprite_info 聚合 + screenshot --node 边界计算（issue #101）
##
## 全部纯属性读 / 坐标变换，headless（dummy renderer）下可测；
## 真正取图 + 裁剪像素的链路由 python/tests/test_e2e_screenshot_gui.py
## （macOS GUI 格）兜底。
extends GutTest

const RenderApiScript := preload("res://addons/godot_cli_control/bridge/render_api.gd")

var _api: Node


func before_each() -> void:
	_api = RenderApiScript.new()
	_api.name = "RenderApi"
	add_child_autofree(_api)


## 8x4 棋盘格贴图：尺寸可被 hframes/vframes 整除，effective_region 断言用整数。
func _make_texture(width: int = 8, height: int = 4) -> ImageTexture:
	var img := Image.create_empty(width, height, false, Image.FORMAT_RGBA8)
	img.fill(Color.RED)
	return ImageTexture.create_from_image(img)


func _add_sprite(tex: Texture2D) -> Sprite2D:
	var sprite := Sprite2D.new()
	sprite.texture = tex
	add_child_autofree(sprite)
	return sprite


# ── sprite_info：Sprite2D ────────────────────────────────────────────


func test_sprite_info_node_not_found_returns_1001() -> void:
	var result: Dictionary = _api.handle_sprite_info({"path": "/root/__missing__"})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1001)


func test_sprite_info_unsupported_type_returns_1010() -> void:
	var plain := Node2D.new()
	plain.name = "PlainNode2D"
	add_child_autofree(plain)
	var result: Dictionary = _api.handle_sprite_info({"path": str(plain.get_path())})
	assert_has(result, "error")
	assert_eq(int(result.error.code), 1010)
	assert_string_contains(str(result.error.message), "Node2D")


func test_sprite_info_sprite2d_basic_fields() -> void:
	var sprite := _add_sprite(_make_texture())
	sprite.flip_h = true
	sprite.modulate = Color(1.0, 0.5, 0.25, 1.0)
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_does_not_have(info, "error")
	assert_eq(str(info.type), "Sprite2D")
	assert_true(bool(info.visible))
	assert_true(bool(info.flip_h))
	assert_false(bool(info.flip_v))
	assert_almost_eq(float(info.modulate[1]), 0.5, 0.001)
	assert_eq(info.texture.size, [8, 4])
	# 运行时建的贴图无 resource_path → null（与「忘了设贴图」可区分：texture 本身非 null）
	assert_null(info.texture.path)


func test_sprite_info_frame_grid_effective_region() -> void:
	# 8x4 贴图切 4x2 网格 → 每帧 2x2；frame=5 → 坐标 (1,1) → region [2,2,2,2]
	var sprite := _add_sprite(_make_texture())
	sprite.hframes = 4
	sprite.vframes = 2
	sprite.frame = 5
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_eq(int(info.frame), 5)
	assert_eq(info.frame_coords, [1, 1])
	assert_false(bool(info.region_enabled))
	assert_eq(info.effective_region, [2.0, 2.0, 2.0, 2.0])


func test_sprite_info_region_wins_over_frame_grid() -> void:
	var sprite := _add_sprite(_make_texture())
	sprite.hframes = 4
	sprite.region_enabled = true
	sprite.region_rect = Rect2(1, 0, 3, 4)
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_true(bool(info.region_enabled))
	assert_eq(info.effective_region, [1.0, 0.0, 3.0, 4.0])


func test_sprite_info_no_texture_effective_region_null() -> void:
	var sprite := _add_sprite(null)
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_null(info.texture)
	assert_null(info.effective_region)


# ── sprite_info：AnimatedSprite2D ────────────────────────────────────


func _add_animated(frame_count: int = 2) -> AnimatedSprite2D:
	var frames := SpriteFrames.new()
	# SpriteFrames.new() 自带 "default" 动画；直接往里填帧
	for i in frame_count:
		frames.add_frame("default", _make_texture(4 + i * 2, 4))
	var sprite := AnimatedSprite2D.new()
	sprite.sprite_frames = frames
	add_child_autofree(sprite)
	return sprite


func test_sprite_info_animated_frame_texture_tracks_current_frame() -> void:
	var sprite := _add_animated(2)
	sprite.frame = 1
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_eq(str(info.type), "AnimatedSprite2D")
	assert_eq(str(info.animation), "default")
	assert_eq(int(info.frame), 1)
	assert_false(bool(info.playing))
	# 帧 1 的贴图是 6x4 —— frame_texture 跟着当前帧走，这就是 FRONT/BACK 区分的抓手
	assert_eq(info.frame_texture.size, [6, 4])


func test_sprite_info_animated_atlas_frame_reports_atlas_region() -> void:
	var atlas_source := _make_texture(8, 4)
	var atlas_tex := AtlasTexture.new()
	atlas_tex.atlas = atlas_source
	atlas_tex.region = Rect2(4, 0, 4, 4)
	var frames := SpriteFrames.new()
	frames.add_frame("default", atlas_tex)
	var sprite := AnimatedSprite2D.new()
	sprite.sprite_frames = frames
	add_child_autofree(sprite)
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_eq(info.frame_texture.atlas_region, [4.0, 0.0, 4.0, 4.0])


func test_sprite_info_animated_without_frames_resource() -> void:
	var sprite := AnimatedSprite2D.new()
	add_child_autofree(sprite)
	var info: Dictionary = _api.handle_sprite_info({"path": str(sprite.get_path())})
	assert_does_not_have(info, "error")
	assert_null(info.sprite_frames)
	assert_null(info.frame_texture)


# ── sprite_info：TextureRect ─────────────────────────────────────────


func test_sprite_info_texture_rect() -> void:
	var rect := TextureRect.new()
	rect.texture = _make_texture()
	rect.flip_v = true
	rect.size = Vector2(40, 20)
	add_child_autofree(rect)
	var info: Dictionary = _api.handle_sprite_info({"path": str(rect.get_path())})
	assert_eq(str(info.type), "TextureRect")
	assert_true(bool(info.flip_v))
	assert_eq(info.texture.size, [8, 4])
	assert_eq(info.size, [40.0, 20.0])
	assert_has(info, "stretch_mode")


# ── compute_node_screen_rect（screenshot --node 的边界计算） ──────────


func test_screen_rect_control_uses_global_position_and_size() -> void:
	var ctl := Control.new()
	ctl.position = Vector2(10, 20)
	ctl.size = Vector2(30, 40)
	add_child_autofree(ctl)
	var rect: Variant = RenderApiScript.compute_node_screen_rect(ctl)
	assert_true(rect is Rect2, "Control 应能算出 Rect2，实际: %s" % [rect])
	assert_eq(rect as Rect2, Rect2(10, 20, 30, 40))


func test_screen_rect_sprite2d_centered_with_scale() -> void:
	# 8x4 贴图 centered + position(100,50) + scale 2 → AABB (92,46,16,8)
	var sprite := _add_sprite(_make_texture())
	sprite.position = Vector2(100, 50)
	sprite.scale = Vector2(2, 2)
	var rect: Variant = RenderApiScript.compute_node_screen_rect(sprite)
	assert_true(rect is Rect2)
	assert_eq(rect as Rect2, Rect2(92, 46, 16, 8))


func test_screen_rect_applies_viewport_final_transform() -> void:
	# content scale（hiDPI / stretch 窗口）回归（#137）：rect 必须落在取图侧
	# get_image() 的物理像素系。SubViewport size(200,100) + size_2d_override(100,50)
	# + stretch 构造 final transform = scale 2：Control (10,20,30,40) → (20,40,60,80)。
	# 漏乘 final transform 时返回画布坐标 (10,20,30,40)，hiDPI 下裁剪整体错位。
	var viewport := SubViewport.new()
	viewport.size = Vector2i(200, 100)
	viewport.size_2d_override = Vector2i(100, 50)
	viewport.size_2d_override_stretch = true
	add_child_autofree(viewport)
	var ctl := Control.new()
	ctl.position = Vector2(10, 20)
	ctl.size = Vector2(30, 40)
	viewport.add_child(ctl)
	var rect: Variant = RenderApiScript.compute_node_screen_rect(ctl)
	assert_true(rect is Rect2, "stretch viewport 内 Control 应能算出 Rect2，实际: %s" % [rect])
	assert_eq(rect as Rect2, Rect2(20, 40, 60, 80))


func test_screen_rect_non_canvas_item_returns_1010() -> void:
	var plain := Node.new()
	plain.name = "PlainNode"
	add_child_autofree(plain)
	var result: Variant = RenderApiScript.compute_node_screen_rect(plain)
	assert_true(result is Dictionary)
	assert_eq(int((result as Dictionary).error.code), 1010)


func test_screen_rect_sprite_without_texture_returns_1010() -> void:
	# Sprite2D.get_rect() 无贴图时返回零尺寸 rect —— 仍是 Rect2，
	# 1011（屏幕外/零尺寸）的判定留给截图交集阶段；这里只验证不崩。
	var animated := AnimatedSprite2D.new()
	add_child_autofree(animated)
	var result: Variant = RenderApiScript.compute_node_screen_rect(animated)
	assert_true(result is Dictionary, "无 frames 的 AnimatedSprite2D 算不出边界 → 1010")
	assert_eq(int((result as Dictionary).error.code), 1010)
