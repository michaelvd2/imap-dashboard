from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from imap_dashboard.server import OPERATOR_TOOLS, READ_TOOLS, build_server
from scripts import install_dashboard
from scripts.install_dashboard import _updated_marketplace
from scripts.privacy_scan import scan as privacy_scan
from scripts.verify_backend import BackendVerificationError, verify as verify_backend


ROOT = Path(__file__).resolve().parents[1]


def payload(result):
    assert result.is_error is False
    return json.loads(result.content[0].text)


class StubBridge:
    profile = "read"
    settings = SimpleNamespace(account_id="demo", from_address="mailbox@example.test")


class StubOperator:
    def __init__(self):
        self.sessions = 0
        self.assistant_reads = 0

    def create_ui_session(self):
        self.sessions += 1
        return "ui-session"

    def get_assistant_result(self, **kwargs):
        self.assistant_reads += 1
        return {"hit": False, "action": kwargs["action"], "encrypted_with_native_keystore": True}


def test_dashboard_tool_surface_is_visual_adapter_without_setup_or_raw_credentials():
    read_names = {tool.name for tool in asyncio.run(build_server(bridge=StubBridge(), operator=StubOperator()).list_tools())}
    assert read_names == set(READ_TOOLS)
    assert not read_names.intersection({"open_setup", "raw_imap", "read_secret", "permanent_delete", "expunge"})

    write_bridge = StubBridge()
    write_bridge.profile = "operator"
    write_names = {
        tool.name
        for tool in asyncio.run(
            build_server(profile="operator", bridge=write_bridge, operator=StubOperator()).list_tools()
        )
    }
    assert write_names == set(READ_TOOLS + OPERATOR_TOOLS)


def test_opening_dashboard_only_creates_ui_session():
    operator = StubOperator()
    result = asyncio.run(build_server(bridge=StubBridge(), operator=operator).call_tool("render_mail_view", {}))
    value = payload(result)
    assert operator.sessions == 1
    assert operator.assistant_reads == 0
    assert value["presentation"] == {
        "surface": "side-panel",
        "host_display_mode": "fullscreen",
        "allow_inline": False,
    }


def test_dashboard_maps_ui_cache_to_existing_protected_connection():
    operator = StubOperator()
    result = asyncio.run(
        build_server(bridge=StubBridge(), operator=operator).call_tool(
            "get_brain_result",
            {
                "ui_session_id": "ui-session",
                "message_ref": {"account_id": "demo", "folder_id": "inbox", "uidvalidity": 1, "uid": 2},
                "action": "actions",
            },
        )
    )
    assert payload(result)["encrypted_with_native_keystore"] is True
    assert operator.assistant_reads == 1


def test_marketplace_update_preserves_imap_plugin_and_unrelated_entries():
    backend = {
        "name": "imap-plugin",
        "source": {"source": "local", "path": "./plugins/imap-plugin"},
    }
    unrelated = {"name": "another-plugin", "source": {"source": "local", "path": "./plugins/another"}}
    existing_dashboard = {"name": "imap-dashboard", "source": {"source": "local", "path": "./old"}}
    original = {"name": "imap-plugin-handoff", "plugins": [backend, unrelated, existing_dashboard]}
    updated = _updated_marketplace(original)

    assert updated["plugins"][0:2] == [backend, unrelated]
    assert updated["plugins"][-1]["name"] == "imap-dashboard"
    assert updated["plugins"][-1]["source"]["path"] == "./plugins/imap-dashboard"


def test_dashboard_contains_no_connector_or_credential_implementation():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src").rglob("*.py"))
    )
    assert "from imap_plugin.bridge import MailBridge" in source
    assert "from imap_plugin.operator import MailOperator" in source
    assert "WindowsCredentialStore" not in source
    assert "MacKeychainCredentialStore" not in source
    assert "def read_secret" not in source
    assert not (ROOT / "src" / "imap_dashboard" / "config.py").exists()
    assert not (ROOT / "src" / "imap_dashboard" / "credentials.py").exists()


def test_launchers_resolve_one_sibling_imap_plugin():
    windows = (ROOT / "scripts" / "launch.cmd").read_text(encoding="utf-8").casefold()
    macos = (ROOT / "scripts" / "launch_macos.sh").read_text(encoding="utf-8").casefold()
    assert "..\\imap-plugin" in windows
    assert "../imap-plugin" in macos
    assert "imap_dashboard.server" in windows
    assert "imap_dashboard.server" in macos
    assert "credential" not in windows
    assert "credential" not in macos


def test_macos_generated_manifest_points_to_final_dashboard_path(monkeypatch, tmp_path):
    stage = tmp_path / "plugins" / ".new"
    destination = tmp_path / "plugins" / "imap-dashboard"
    backend = tmp_path / "plugins" / "imap-plugin"
    python = backend / "runtime" / "venv" / "bin" / "python"
    stage.mkdir(parents=True)
    monkeypatch.setattr(install_dashboard.platform, "system", lambda: "Darwin")

    install_dashboard._write_platform_manifest(stage, destination, backend, python)
    value = json.loads((stage / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["imap-dashboard"]

    assert value["cwd"] == str(destination)
    assert str(destination / "src") in value["env"]["PYTHONPATH"]
    assert str(stage) not in value["env"]["PYTHONPATH"]


def test_installer_adds_dashboard_without_touching_existing_state(monkeypatch, tmp_path):
    program_root = tmp_path / "IMAP Plugin"
    distribution = program_root / "distribution"
    backend = distribution / "plugins" / "imap-plugin"
    python = backend / "runtime" / "python" / "python.exe"
    (backend / ".codex-plugin").mkdir(parents=True)
    (backend / "src" / "imap_plugin").mkdir(parents=True)
    python.parent.mkdir(parents=True)
    (backend / ".codex-plugin" / "plugin.json").write_text('{"name":"imap-plugin"}', encoding="utf-8")
    (backend / "src" / "imap_plugin" / "server.py").write_text("# verified fixture\n", encoding="utf-8")
    python.write_bytes(b"fixture")
    marketplace = distribution / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        json.dumps(
            {
                "name": "imap-plugin-handoff",
                "plugins": [
                    {
                        "name": "imap-plugin",
                        "source": {"source": "local", "path": "./plugins/imap-plugin"},
                    },
                    {
                        "name": "unrelated",
                        "source": {"source": "local", "path": "./plugins/unrelated"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    protected_state = program_root / "shared-state-do-not-touch.bin"
    protected_state.write_bytes(b"unchanged")
    monkeypatch.setattr(install_dashboard.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install_dashboard.sys, "executable", str(python))
    monkeypatch.setattr(install_dashboard, "_verify_imports", lambda *args: None)

    receipt = install_dashboard.install(ROOT, program_root)

    assert receipt["reused_existing_connection"] is True
    assert receipt["credentials_changed"] is False
    assert receipt["configuration_changed"] is False
    assert protected_state.read_bytes() == b"unchanged"
    assert (distribution / "plugins" / "imap-dashboard" / "src" / "imap_dashboard" / "server.py").is_file()
    names = [entry["name"] for entry in json.loads(marketplace.read_text(encoding="utf-8"))["plugins"]]
    assert names == ["imap-plugin", "unrelated", "imap-dashboard"]


def test_macos_integrity_manifest_is_checked_before_backend_code(monkeypatch, tmp_path):
    program_root = tmp_path / "IMAP Plugin"
    distribution = program_root / "distribution"
    backend = distribution / "plugins" / "imap-plugin"
    (backend / ".codex-plugin").mkdir(parents=True)
    (backend / "src" / "imap_plugin").mkdir(parents=True)
    (backend / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"imap-plugin","version":"0.1.3"}', encoding="utf-8"
    )
    server = backend / "src" / "imap_plugin" / "server.py"
    server.write_text("# fixture that must never execute\n", encoding="utf-8")
    files = []
    for path in sorted(item for item in backend.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(distribution).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
            }
        )
    (distribution / "backend-integrity.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "product": "imap-plugin-installed-backend",
                "version": "0.1.3",
                "platform": "macos",
                "root": "plugins/imap-plugin",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.verify_backend.platform.system", lambda: "Darwin")

    assert verify_backend(program_root)["backend_code_executed"] is False
    server.write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(BackendVerificationError, match="integrity check failed"):
        verify_backend(program_root)


def test_platform_installers_verify_before_launching_backend():
    windows = (ROOT / "install.ps1").read_text(encoding="utf-8")
    windows_verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8")
    macos = (ROOT / "scripts" / "install_macos.sh").read_text(encoding="utf-8")
    assert windows.index("verify.ps1") < windows.index("doctor")
    assert "verify_backend.ps1" in windows_verifier
    assert "runtime\\python\\python.exe" not in windows_verifier
    assert macos.index("verify_backend.py") < macos.index('\n"$doctor" >/dev/null')


def test_privacy_scan_does_not_skip_a_package_because_its_parent_is_dist(tmp_path):
    stage = tmp_path / "dist" / "package"
    stage.mkdir(parents=True)
    readme = stage / "README.md"
    readme.write_text("customer safe", encoding="utf-8")
    assert privacy_scan(stage) == []
    readme.write_text("Mich" + "ael", encoding="utf-8")
    assert privacy_scan(stage) == ["README.md: private marker"]
