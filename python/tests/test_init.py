"""init_cmd 单元测试 —— project.godot patch 与端到端接入流程。"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_cli_control.init_cmd import (
    _ensure_in_packed_array,
    _ensure_kv_in_section,
    locate_plugin_source,
    run_init,
)


# ── _ensure_kv_in_section ──


def test_kv_adds_section_when_missing() -> None:
    text = "config_version=5\n\n[application]\nconfig/name=\"x\"\n"
    out, changed = _ensure_kv_in_section(text, "autoload", "Foo", 'Foo="*res://x"')
    assert changed is True
    assert "[autoload]" in out
    assert 'Foo="*res://x"' in out


def test_kv_appends_when_section_exists_without_key() -> None:
    text = '[autoload]\nOther="*res://other.gd"\n'
    out, changed = _ensure_kv_in_section(
        text, "autoload", "GameBridgeNode", 'GameBridgeNode="*res://gb.gd"'
    )
    assert changed is True
    assert 'Other="*res://other.gd"' in out
    assert 'GameBridgeNode="*res://gb.gd"' in out


def test_kv_idempotent_when_key_exists() -> None:
    text = '[autoload]\nGameBridgeNode="*res://existing.gd"\n'
    out, changed = _ensure_kv_in_section(
        text, "autoload", "GameBridgeNode", 'GameBridgeNode="*res://NEW.gd"'
    )
    assert changed is False
    assert out == text  # 不动


# ── _ensure_in_packed_array ──


def test_packed_array_adds_section_when_missing() -> None:
    text = "config_version=5\n"
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is True
    assert (
        'enabled=PackedStringArray("res://addons/x/plugin.cfg")' in out
    )


def test_packed_array_appends_to_existing_array() -> None:
    text = (
        "[editor_plugins]\n"
        'enabled=PackedStringArray("res://addons/foo/plugin.cfg")\n'
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/bar/plugin.cfg"
    )
    assert changed is True
    assert "res://addons/foo/plugin.cfg" in out
    assert "res://addons/bar/plugin.cfg" in out


def test_packed_array_idempotent_when_value_present() -> None:
    text = (
        "[editor_plugins]\n"
        'enabled=PackedStringArray("res://addons/x/plugin.cfg")\n'
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is False
    assert out == text


def test_packed_array_handles_multiline_godot_format() -> None:
    """Godot 长插件列表 / 手工编辑可能折行；regex 必须跨行匹配。

    防 regression：单行 ``.*?`` 不带 DOTALL 会 miss 多行格式 → 落到 fallback
    分支追加新 ``enabled=`` 行，把原列表 last-wins 覆盖丢失。
    """
    text = (
        "[editor_plugins]\n"
        "enabled=PackedStringArray(\n"
        '    "res://addons/foo/plugin.cfg",\n'
        '    "res://addons/bar/plugin.cfg",\n'
        ")\n"
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/baz/plugin.cfg"
    )
    assert changed is True
    # 三个值都在，且只有一个 enabled= 行
    assert out.count("enabled=") == 1, f"重复了 enabled 行：\n{out}"
    assert "res://addons/foo/plugin.cfg" in out
    assert "res://addons/bar/plugin.cfg" in out
    assert "res://addons/baz/plugin.cfg" in out


def test_packed_array_idempotent_on_multiline_when_value_present() -> None:
    text = (
        "[editor_plugins]\n"
        "enabled=PackedStringArray(\n"
        '    "res://addons/x/plugin.cfg"\n'
        ")\n"
    )
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is False
    assert out == text


def test_packed_array_creates_key_when_section_has_other_keys() -> None:
    text = "[editor_plugins]\nsomething=else\n"
    out, changed = _ensure_in_packed_array(
        text, "editor_plugins", "enabled", "res://addons/x/plugin.cfg"
    )
    assert changed is True
    assert "something=else" in out
    assert 'enabled=PackedStringArray("res://addons/x/plugin.cfg")' in out


# ── 端到端 run_init ──


def _minimal_project(tmp_path: Path) -> Path:
    """造一个最小 Godot 项目根。"""
    (tmp_path / "project.godot").write_text(
        "config_version=5\n\n"
        "[application]\n"
        'config/name="dummy"\n'
        'config/features=PackedStringArray("4.4", "GL Compatibility")\n'
    )
    return tmp_path


def test_run_init_rejects_non_godot_dir(tmp_path: Path, capsys) -> None:
    rc = run_init(tmp_path)
    assert rc == 1
    assert "project.godot" in capsys.readouterr().err


def test_run_init_creates_addons_and_patches_project_godot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快乐路径：在干净项目上跑一次，断言所有产物。"""
    # 让 init 找不到 GODOT_BIN，避免污染本地 .cli_control
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = _minimal_project(tmp_path)
    rc = run_init(project)
    assert rc == 0

    plugin_dst = project / "addons" / "godot_cli_control"
    assert (plugin_dst / "plugin.cfg").is_file()
    assert (plugin_dst / "bridge" / "game_bridge.gd").is_file()

    pg = (project / "project.godot").read_text()
    assert "[autoload]" in pg
    assert 'GameBridgeNode="*res://addons/godot_cli_control/bridge/game_bridge.gd"' in pg
    assert "[editor_plugins]" in pg
    assert 'enabled=PackedStringArray("res://addons/godot_cli_control/plugin.cfg")' in pg


def test_run_init_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复跑 init 不能破坏 project.godot 或抛错。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = _minimal_project(tmp_path)
    assert run_init(project) == 0
    pg_after_first = (project / "project.godot").read_text()

    # 第二次：应该 noop
    assert run_init(project) == 0
    pg_after_second = (project / "project.godot").read_text()
    assert pg_after_first == pg_after_second


def test_run_init_writes_godot_bin_when_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检测到 Godot 时应写 .cli_control/godot_bin。"""
    fake_bin = tmp_path / "godot_fake"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary",
        lambda: str(fake_bin),
    )
    # init 现在会无条件调 reimport_project；用 fake bin 真去 exec 没意义
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.reimport_project",
        lambda *a, **k: None,
    )

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    project = _minimal_project(proj_dir)
    rc = run_init(project)
    assert rc == 0
    saved = (project / ".cli_control" / "godot_bin").read_text().strip()
    assert saved == str(fake_bin)


def test_run_init_preserves_crlf_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 上的 CRLF 项目文件在 patch 后必须仍是 CRLF —— 防止 git diff
    把整个文件标成改动。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )

    project = tmp_path
    crlf_content = (
        "config_version=5\r\n"
        "\r\n"
        "[application]\r\n"
        'config/name="crlf-project"\r\n'
        'config/features=PackedStringArray("4.4")\r\n'
    )
    (project / "project.godot").write_bytes(crlf_content.encode("utf-8"))

    assert run_init(project) == 0
    after = (project / "project.godot").read_bytes()
    # 原 CRLF 行仍是 CRLF；新加的行 _patch_project_godot 也会写成 CRLF
    assert b"\r\nconfig_version=5\r\n" in (b"\r\n" + after) or after.startswith(
        b"config_version=5\r\n"
    )
    assert b"\r\n[autoload]\r\n" in after
    assert b"\r\nGameBridgeNode=" in after
    # 不应混入裸 LF
    bare_lf_count = after.count(b"\n") - after.count(b"\r\n")
    assert bare_lf_count == 0, f"出现裸 LF：{bare_lf_count} 处"


def test_locate_plugin_source_finds_repo_addons() -> None:
    """editable install / 源码模式下应能找到仓库顶层的 addons。"""
    src = locate_plugin_source()
    assert src is not None
    assert (src / "plugin.cfg").is_file()
    assert (src / "bridge" / "game_bridge.gd").is_file()


# ── init 与 skills 的集成 ──


def _make_min_godot_project(tmp_path: Path) -> Path:
    """造一个最小 project.godot —— 满足 run_init 的入口校验。"""
    (tmp_path / "project.godot").write_text(
        "config_version=5\n\n[application]\nconfig/name=\"x\"\n",
        encoding="utf-8",
    )
    return tmp_path


def test_init_writes_both_skills_by_default(tmp_path: Path) -> None:
    """默认 write_skills=True 时两条 SKILL.md 都生成。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj)

    assert rc == 0
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()


def test_init_no_skills_skips_skill_files(tmp_path: Path) -> None:
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    rc = run_init(proj, write_skills=False)

    assert rc == 0
    assert not (proj / CLAUDE_REL).exists()
    assert not (proj / CODEX_REL).exists()


def test_init_skills_only_skips_plugin_and_patch(tmp_path: Path) -> None:
    """skills_only=True：addons/ 不被建立、project.godot 不被改动、SKILL.md 写入。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    original = (proj / "project.godot").read_bytes()

    rc = run_init(proj, skills_only=True)

    assert rc == 0
    assert not (proj / "addons" / "godot_cli_control").exists()
    assert (proj / "project.godot").read_bytes() == original
    assert (proj / CLAUDE_REL).is_file()
    assert (proj / CODEX_REL).is_file()


def test_init_skills_only_still_validates_godot_project(tmp_path: Path) -> None:
    """skills_only 不能绕过 project.godot 入口校验 —— 否则会在非 Godot 目录乱写。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL

    # 不造 project.godot
    rc = run_init(tmp_path, skills_only=True)

    assert rc == 1
    assert not (tmp_path / CLAUDE_REL).exists()


def test_init_skills_no_clobber_preserves_existing(tmp_path: Path) -> None:
    """clobber_skills=False 遇到已存在的 SKILL.md 时保留原内容、补缺另一条。"""
    from godot_cli_control.init_cmd import run_init
    from godot_cli_control.skills_install import CLAUDE_REL, CODEX_REL

    proj = _make_min_godot_project(tmp_path)
    # 预先手写 .claude 那份；.codex 缺失
    (proj / CLAUDE_REL).parent.mkdir(parents=True, exist_ok=True)
    (proj / CLAUDE_REL).write_text("USER_CUSTOM_KEEP_ME", encoding="utf-8")

    rc = run_init(proj, skills_only=True, clobber_skills=False)

    assert rc == 0
    # 已存在那条不动
    assert (
        (proj / CLAUDE_REL).read_text(encoding="utf-8") == "USER_CUSTOM_KEEP_ME"
    )
    # 缺失那条补上
    assert (proj / CODEX_REL).is_file()
    assert "USER_CUSTOM_KEEP_ME" not in (
        proj / CODEX_REL
    ).read_text(encoding="utf-8")


# ── run_init JSON 输出契约（issue #50）──


def test_run_init_json_mode_suppresses_human_prints_and_collects_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``output_format='json'``：stdout 必须完全干净（envelope 由 cli 侧封），
    结构化字段全部回填到 ``result`` dict。"""
    from godot_cli_control.init_cmd import run_init

    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _make_min_godot_project(tmp_path)

    result: dict[str, object] = {}
    rc = run_init(proj, output_format="json", result=result)
    captured = capsys.readouterr()

    assert rc == 0
    # json 模式下 run_init 自身不能往 stdout 写任何东西 —— cli.cmd_init 才负责输出 envelope
    assert captured.out == ""
    # 结构化字段 cli 侧拿来塞 envelope
    assert result["project_root"] == str(proj)
    assert result["plugin_copied"] is True
    assert result["plugin_overwritten"] is False
    assert "autoload/GameBridgeNode" in result["project_godot_changes"]
    assert result["godot_bin"] is None
    assert len(result["skills_written"]) == 2


def test_run_init_json_mode_records_error_message_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """json 模式 + 非 Godot 目录：rc=1，``result[INIT_RESULT_ERROR_KEY]`` 必须有值，
    且不许往 stderr 喷"错误：..."字符串（envelope 是唯一通道）。"""
    from godot_cli_control.init_cmd import INIT_RESULT_ERROR_KEY, run_init

    result: dict[str, object] = {}
    rc = run_init(tmp_path, output_format="json", result=result)
    captured = capsys.readouterr()

    assert rc == 1
    # stdout 必须完全干净——envelope 由 cli 侧封；
    # stderr 不能含项目自定义"错误："标签或 Python traceback
    # （第三方 import warning 之类无害噪声仍允许，避免断言过紧）。
    assert captured.out == ""
    assert "错误：" not in captured.err
    assert "Traceback" not in captured.err
    assert "project.godot" in result[INIT_RESULT_ERROR_KEY]


def test_cmd_init_emits_success_envelope_in_json_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """端到端：``godot-cli-control init --json`` 在快乐路径必须输出单行 envelope。"""
    import argparse
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, cmd_init

    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _make_min_godot_project(tmp_path)

    ns = argparse.Namespace(
        path=str(proj),
        force=False,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=False,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_init(ns)
    out = capsys.readouterr().out.strip()

    assert rc == 0
    # 单行 JSON envelope —— AI agent 的契约根基
    assert out.count("\n") == 0
    payload = _json.loads(out)
    assert payload["ok"] is True
    result = payload["result"]
    assert result["plugin_copied"] is True
    assert isinstance(result["project_godot_changes"], list)
    assert result["skills_only"] is False


def test_cmd_init_emits_error_envelope_on_non_godot_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """非 Godot 项目目录 + ``--json`` → 单行 error envelope，stdout 不含人类字串。"""
    import argparse
    import json as _json

    from godot_cli_control.cli import OUTPUT_JSON, cmd_init

    ns = argparse.Namespace(
        path=str(tmp_path),
        force=False,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=False,
        output_format=OUTPUT_JSON,
    )
    rc = cmd_init(ns)
    out = capsys.readouterr().out.strip()

    assert rc == 1
    payload = _json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == -1003  # CLIENT_CODE_USAGE
    assert "project.godot" in payload["error"]["message"]


# ── .gitignore 维护（确保 .cli_control/ 不被提交）──


def test_gitignore_adds_entry_to_empty_text() -> None:
    """纯函数：空文本追加条目。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    out, added = _add_gitignore_entries("", [".cli_control/"])
    assert added == [".cli_control/"]
    assert out == ".cli_control/\n"
    # 不允许出现前导空行
    assert not out.startswith("\n")


def test_gitignore_appends_preserving_existing_entries() -> None:
    """已有别的忽略项时只追加、不动原内容。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    text = "*.tmp\nbuild/\n"
    out, added = _add_gitignore_entries(text, [".cli_control/"])
    assert added == [".cli_control/"]
    assert "*.tmp" in out
    assert "build/" in out
    assert out.endswith(".cli_control/\n")


def test_gitignore_idempotent_when_entry_present() -> None:
    """已含完全相同条目 → 不重复加。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    text = "build/\n.cli_control/\n"
    out, added = _add_gitignore_entries(text, [".cli_control/"])
    assert added == []
    assert out == text


@pytest.mark.parametrize(
    "existing",
    [".cli_control\n", "/.cli_control/\n", "/.cli_control\n", ".cli_control/  \n"],
)
def test_gitignore_idempotent_on_equivalent_variants(existing: str) -> None:
    """带/不带斜杠、带前导 / 、带尾随空白都视为已忽略，不重复加。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    out, added = _add_gitignore_entries(existing, [".cli_control/"])
    assert added == []
    assert out == existing


def test_gitignore_commented_line_does_not_count_as_present() -> None:
    """被注释掉的同名行不算忽略，真条目仍要补上。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    text = "# .cli_control/\n"
    out, added = _add_gitignore_entries(text, [".cli_control/"])
    assert added == [".cli_control/"]
    assert "# .cli_control/" in out  # 注释保留
    assert out.endswith(".cli_control/\n")


def test_gitignore_appends_newline_when_file_lacks_trailing_one() -> None:
    """原文件末行无换行符时，新条目必须独立成行、不黏在末行后面。"""
    from godot_cli_control.init_cmd import _add_gitignore_entries

    out, added = _add_gitignore_entries("build/", [".cli_control/"])
    assert added == [".cli_control/"]
    assert out == "build/\n.cli_control/\n"


def test_ensure_gitignore_creates_file_when_missing(tmp_path: Path) -> None:
    """项目根没有 .gitignore → 创建并写入 .cli_control/。"""
    from godot_cli_control.init_cmd import _ensure_gitignore_entries

    added = _ensure_gitignore_entries(tmp_path)
    gi = tmp_path / ".gitignore"
    assert gi.is_file()
    assert ".cli_control/" in gi.read_text(encoding="utf-8")
    assert added == [".cli_control/"]


def test_ensure_gitignore_idempotent_across_runs(tmp_path: Path) -> None:
    """连跑两次只写一条，不重复。"""
    from godot_cli_control.init_cmd import _ensure_gitignore_entries

    _ensure_gitignore_entries(tmp_path)
    added2 = _ensure_gitignore_entries(tmp_path)
    assert added2 == []
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".cli_control/") == 1


def test_ensure_gitignore_preserves_crlf(tmp_path: Path) -> None:
    """已有 CRLF 的 .gitignore 在追加后必须仍是 CRLF，不混入裸 LF。"""
    from godot_cli_control.init_cmd import _ensure_gitignore_entries

    gi = tmp_path / ".gitignore"
    gi.write_bytes(b"*.tmp\r\nbuild/\r\n")
    _ensure_gitignore_entries(tmp_path)
    after = gi.read_bytes()
    assert b".cli_control/\r\n" in after
    bare_lf = after.count(b"\n") - after.count(b"\r\n")
    assert bare_lf == 0, f"出现裸 LF：{bare_lf} 处"


def test_run_init_adds_cli_control_to_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：run_init 默认在项目根 .gitignore 忽略 .cli_control/。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _minimal_project(tmp_path)
    assert run_init(proj) == 0
    gi = (proj / ".gitignore").read_text(encoding="utf-8")
    assert ".cli_control/" in gi


def test_run_init_no_gitignore_skips_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_gitignore=False → 不创建 / 不改 .gitignore。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _minimal_project(tmp_path)
    assert run_init(proj, write_gitignore=False) == 0
    assert not (proj / ".gitignore").exists()


def test_run_init_skills_only_skips_gitignore(tmp_path: Path) -> None:
    """skills_only 属于纯刷 SKILL.md，不应碰 .gitignore。"""
    proj = _make_min_godot_project(tmp_path)
    assert run_init(proj, skills_only=True) == 0
    assert not (proj / ".gitignore").exists()


def test_run_init_json_records_gitignore_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """json 模式 result 必须回填 gitignore_added 字段。"""
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _make_min_godot_project(tmp_path)
    result: dict[str, object] = {}
    assert run_init(proj, output_format="json", result=result) == 0
    assert result["gitignore_added"] == [".cli_control/"]


def test_cmd_init_no_gitignore_flag_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 层：--no-gitignore（ns.no_gitignore=True）不写 .gitignore。"""
    import argparse

    from godot_cli_control.cli import OUTPUT_JSON, cmd_init

    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(
        "godot_cli_control.init_cmd.find_godot_binary", lambda: None
    )
    proj = _make_min_godot_project(tmp_path)
    ns = argparse.Namespace(
        path=str(proj),
        force=False,
        no_skills=False,
        skills_only=False,
        skills_no_clobber=False,
        no_gitignore=True,
        output_format=OUTPUT_JSON,
    )
    assert cmd_init(ns) == 0
    capsys.readouterr()
    assert not (proj / ".gitignore").exists()
