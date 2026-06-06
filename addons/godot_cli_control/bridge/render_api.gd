class_name RenderApi
extends Node
## 渲染态查询 API：sprite_info + screenshot --node 的边界计算（issue #101）
##
## 设计动机：bridge 能逐个读基本属性，但「这个节点实际渲染成什么样」
## 需要聚合查询（texture / 图集区域 / 翻转 / 帧号），否则视觉类 e2e 只能
## 退化为读内部记账字段 + 人工视觉自查兜底。
##
## sprite_info 纯读属性，headless（dummy renderer）下完全可用；
## compute_node_screen_rect 只做坐标变换，同样不依赖真渲染——
## 真正需要 GPU 的只有 screenshot 的取图部分（low_level_api 负责）。


func handle_sprite_info(params: Dictionary) -> Dictionary:
	var path: String = params.get("path", "") as String
	var node: Node = get_tree().root.get_node_or_null(path)
	if node == null:
		return _err(CliControlErrorCodes.NODE_NOT_FOUND, "Node not found: %s" % path)
	# 注意顺序无依赖：Sprite2D / AnimatedSprite2D / TextureRect 互不为子类
	if node is Sprite2D:
		return _sprite2d_info(node as Sprite2D)
	if node is AnimatedSprite2D:
		return _animated_sprite2d_info(node as AnimatedSprite2D)
	if node is TextureRect:
		return _texture_rect_info(node as TextureRect)
	return _err(
		CliControlErrorCodes.UNSUPPORTED_NODE_TYPE,
		"sprite_info: unsupported node type %s (supported: Sprite2D, AnimatedSprite2D, TextureRect)"
			% node.get_class(),
	)


## screenshot --node 的边界计算：节点 local rect → 经 canvas/camera + content
## scale 变换的物理像素坐标 AABB。返回 Rect2，失败返回 error Dictionary（1010）。
## static：只依赖传入节点自身的变换链，便于 GUT 直接单测（无需真渲染）。
static func compute_node_screen_rect(node: Node) -> Variant:
	if not (node is CanvasItem):
		return {
			"error": {
				"code": CliControlErrorCodes.UNSUPPORTED_NODE_TYPE,
				"message": "screenshot node: %s is not a CanvasItem (2D/Control only)"
					% node.get_class(),
			}
		}
	var local_rect_or_null: Variant = _local_rect_of(node as CanvasItem)
	if local_rect_or_null == null:
		return {
			"error": {
				"code": CliControlErrorCodes.UNSUPPORTED_NODE_TYPE,
				"message": "screenshot node: cannot determine bounds for %s (no size/rect/texture)"
					% node.get_class(),
			}
		}
	# get_global_transform_with_canvas 含 CanvasLayer / Camera2D 变换，结果是画布
	# （逻辑设计分辨率）坐标；再乘 viewport.get_final_transform()（content scale 的
	# stretch 缩放 + letterbox 偏移）才与取图侧 get_image() 的物理像素系对齐——
	# 漏乘时 hiDPI / 拉伸窗口下裁剪整体偏左上且偏小（#137）。平窗（物理 == 逻辑）
	# 环境 final transform 为 identity，行为不变。Transform2D * Rect2 返回旋转后
	# 四角的 AABB。
	var xform: Transform2D = (node as CanvasItem).get_global_transform_with_canvas()
	var viewport: Viewport = (node as CanvasItem).get_viewport()
	if viewport != null:
		xform = viewport.get_final_transform() * xform
	return xform * (local_rect_or_null as Rect2)


## 节点自身坐标系下的绘制边界。返回 Rect2 或 null（无法确定）。
## Control 必须先于 has_method("get_rect") 判断：Control.get_rect() 返回的是
## 父坐标系 rect（position+size），不是 local——直接用会双重叠加自身位移。
static func _local_rect_of(item: CanvasItem) -> Variant:
	if item is Control:
		return Rect2(Vector2.ZERO, (item as Control).size)
	if item is AnimatedSprite2D:
		return _animated_sprite2d_local_rect(item as AnimatedSprite2D)
	if item.has_method("get_rect"):
		# Sprite2D（含 centered/offset/region 折算）、TouchScreenButton 等
		return item.call("get_rect")
	return null


## AnimatedSprite2D 没有 get_rect()——按当前帧贴图尺寸 + centered/offset
## 镜像 Sprite2D.get_rect 的语义手工折算。
static func _animated_sprite2d_local_rect(sprite: AnimatedSprite2D) -> Variant:
	var frames: SpriteFrames = sprite.sprite_frames
	if frames == null or not frames.has_animation(sprite.animation):
		return null
	if sprite.frame < 0 or sprite.frame >= frames.get_frame_count(sprite.animation):
		return null
	var tex: Texture2D = frames.get_frame_texture(sprite.animation, sprite.frame)
	if tex == null:
		return null
	var size: Vector2 = tex.get_size()
	var pos: Vector2 = sprite.offset
	if sprite.centered:
		pos -= size / 2.0
	return Rect2(pos, size)


# ── 各类型的聚合 payload ──────────────────────────────────────────────


func _sprite2d_info(sprite: Sprite2D) -> Dictionary:
	var info: Dictionary = _common_info(sprite)
	info["texture"] = _texture_info(sprite.texture)
	info["flip_h"] = sprite.flip_h
	info["flip_v"] = sprite.flip_v
	info["frame"] = sprite.frame
	info["frame_coords"] = [sprite.frame_coords.x, sprite.frame_coords.y]
	info["hframes"] = sprite.hframes
	info["vframes"] = sprite.vframes
	info["region_enabled"] = sprite.region_enabled
	info["region_rect"] = _rect_to_array(sprite.region_rect)
	# effective_region：实际绘制用到的图集区域（视觉断言的核心字段）。
	# region_enabled 时 region 即源（Godot 此时忽略 frame 网格）；
	# 否则按 frame_coords 在 hframes×vframes 网格上折算。
	if sprite.texture != null:
		if sprite.region_enabled:
			info["effective_region"] = _rect_to_array(sprite.region_rect)
		else:
			var fw: float = float(sprite.texture.get_width()) / sprite.hframes
			var fh: float = float(sprite.texture.get_height()) / sprite.vframes
			info["effective_region"] = [
				sprite.frame_coords.x * fw, sprite.frame_coords.y * fh, fw, fh,
			]
	else:
		info["effective_region"] = null
	return info


func _animated_sprite2d_info(sprite: AnimatedSprite2D) -> Dictionary:
	var info: Dictionary = _common_info(sprite)
	var frames: SpriteFrames = sprite.sprite_frames
	info["sprite_frames"] = _resource_path_or_null(frames)
	info["animation"] = String(sprite.animation)
	info["frame"] = sprite.frame
	info["playing"] = sprite.is_playing()
	info["speed_scale"] = sprite.speed_scale
	info["flip_h"] = sprite.flip_h
	info["flip_v"] = sprite.flip_v
	# frame_texture：当前帧实际贴图（FRONT/BACK 这类「换帧不换属性」的
	# 视觉态区分就靠它——内部记账字段读不到的部分）。
	var frame_tex: Texture2D = null
	if (
		frames != null
		and frames.has_animation(sprite.animation)
		and sprite.frame >= 0
		and sprite.frame < frames.get_frame_count(sprite.animation)
	):
		frame_tex = frames.get_frame_texture(sprite.animation, sprite.frame)
	info["frame_texture"] = _texture_info(frame_tex)
	return info


func _texture_rect_info(rect: TextureRect) -> Dictionary:
	var info: Dictionary = _common_info(rect)
	info["texture"] = _texture_info(rect.texture)
	info["flip_h"] = rect.flip_h
	info["flip_v"] = rect.flip_v
	info["stretch_mode"] = rect.stretch_mode
	info["expand_mode"] = rect.expand_mode
	info["size"] = [rect.size.x, rect.size.y]
	return info


func _common_info(item: CanvasItem) -> Dictionary:
	return {
		"type": item.get_class(),
		"visible": item.visible,
		"visible_in_tree": item.is_visible_in_tree(),
		"modulate": [
			item.modulate.r, item.modulate.g, item.modulate.b, item.modulate.a,
		],
	}


## Texture2D → {"path", "size"}；AtlasTexture 额外给 atlas + atlas_region
## （SpriteFrames 常用图集切片建帧，此时帧贴图自身无 resource_path，
## 真正可断言的是「哪张图集的哪块区域」）。无贴图返回 null。
static func _texture_info(tex: Texture2D) -> Variant:
	if tex == null:
		return null
	var info: Dictionary = {
		"path": _resource_path_or_null(tex),
		"size": [tex.get_width(), tex.get_height()],
	}
	if tex is AtlasTexture:
		var atlas_tex: AtlasTexture = tex as AtlasTexture
		info["atlas"] = _resource_path_or_null(atlas_tex.atlas)
		info["atlas_region"] = _rect_to_array(atlas_tex.region)
	return info


static func _resource_path_or_null(res: Resource) -> Variant:
	if res == null or res.resource_path.is_empty():
		return null
	return res.resource_path


static func _rect_to_array(rect: Rect2) -> Array:
	return [rect.position.x, rect.position.y, rect.size.x, rect.size.y]


func _err(code: int, message: String) -> Dictionary:
	return {"error": {"code": code, "message": message}}
