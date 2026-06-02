extends CharacterBody2D
## Demo-controllable character.
##
## Driven externally by godot-cli-control (`tap jump`, `hold move_right`) and
## also playable by hand (Space / Right-arrow). `jump_count` and `moved_right`
## are deliberately exposed as plain vars so a black-box test can assert on them,
## e.g. `godot-cli-control get /root/Main/World/Player jump_count`.

const SPEED := 220.0
const JUMP_VELOCITY := -420.0

## Black-box assertable state: incremented on every successful jump.
var jump_count: int = 0
## Set true once the character has moved right for at least one frame.
var moved_right: bool = false
## Stays idle until the Start button is pressed (see main.gd).
var active: bool = false

@onready var _gravity: float = ProjectSettings.get_setting("physics/2d/default_gravity", 980.0)


func _ready() -> void:
	# Bind keyboard keys to the actions in code, using GDScript key constants, so
	# a human can play too. This avoids hand-writing InputEventKey serialization
	# in project.godot. godot-cli-control injects by action name and does not
	# depend on these bindings.
	_bind_key(&"jump", KEY_SPACE)
	_bind_key(&"move_right", KEY_RIGHT)


func _bind_key(action: StringName, keycode: Key) -> void:
	if not InputMap.has_action(action):
		InputMap.add_action(action)
	var ev := InputEventKey.new()
	ev.physical_keycode = keycode
	InputMap.action_add_event(action, ev)


func _physics_process(delta: float) -> void:
	if not active:
		return
	if not is_on_floor():
		velocity.y += _gravity * delta
	if is_on_floor() and Input.is_action_just_pressed(&"jump"):
		velocity.y = JUMP_VELOCITY
		jump_count += 1
	if Input.is_action_pressed(&"move_right"):
		velocity.x = SPEED
		moved_right = true
	else:
		velocity.x = 0.0
	move_and_slide()
