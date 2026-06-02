# Security Policy

## Threat model in one paragraph

`godot-cli-control` exposes a JSON-RPC server inside a running Godot project so that a local
process can drive the scene. By design that server is **development/test tooling only**:

- it always binds `127.0.0.1` (never a routable interface);
- it is **off by default** even when the plugin is enabled, and must be explicitly activated;
- it is **unconditionally disabled in release/exported builds**;
- a method/property **blacklist** is the last line of defense against turning RPC into arbitrary
  code execution, and third parties may only extend it *additively* via the
  `godot_cli_control/method_blacklist_extra` ProjectSetting.

The full model is documented in the
[plugin README → Security Model](https://github.com/ClaymanTwinkle/godot-cli-control/blob/main/addons/godot_cli_control/README.md#security-model).
A vulnerability, for the purposes of this policy, is anything that breaks one of those guarantees —
e.g. the server binding a non-loopback address, the bridge surviving into a release build, or a
blacklist bypass that reaches a dangerous engine method.

## Supported versions

This project is in **alpha**. Security fixes land on `main` and ship in the next release on PyPI.
Only the latest released version is supported — please reproduce on the latest version before
reporting.

## Reporting a vulnerability

**Please do not open a public issue for a suspected vulnerability.**

Preferred: use GitHub's private vulnerability reporting —
[**Report a vulnerability**](https://github.com/ClaymanTwinkle/godot-cli-control/security/advisories/new)
(repo → **Security** tab → *Report a vulnerability*). This keeps the report private until a fix is
ready and lets us collaborate on a patch and advisory.

If you cannot use that, email **claymantwinkle@gmail.com** with `[security]` in the subject.

Please include:

- affected version (`godot-cli-control --version`) and Godot version;
- a description of the broken guarantee and its impact;
- a minimal reproduction (command sequence, project setup, or script).

## What to expect

This is maintained by a single author on a best-effort basis. We'll acknowledge a valid report,
work with you on a fix and a coordinated disclosure timeline, and credit you in the advisory unless
you prefer to remain anonymous.
