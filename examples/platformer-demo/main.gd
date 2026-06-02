extends Node2D
## Menu logic: pressing Start reveals and activates the player, hides the menu.

@onready var _player: CharacterBody2D = $World/Player
@onready var _start_button: Button = $UI/StartButton


func _ready() -> void:
	_start_button.pressed.connect(_on_start_pressed)


func _on_start_pressed() -> void:
	_player.visible = true
	_player.active = true
	_start_button.hide()
