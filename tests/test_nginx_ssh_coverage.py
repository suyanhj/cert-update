import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deploys.base import DeployTarget
from app.deploys.nginx_ssh import NginxSSHDeployer, NginxServerConfig


class FakeStream:
    def __init__(self, data="", exit_code=0):
        self._data = data
        self.channel = SimpleNamespace(recv_exit_status=lambda: exit_code)

    def read(self):
        return self._data.encode()


class FakeFile:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def write(self, data):
        self.store[self.path] = data


class FakeSFTP:
    def __init__(self, store, existing=None):
        self.store = store
        self.existing = set(existing or [])
        self.closed = False
        self.chmods = []

    def stat(self, path):
        if path not in self.existing:
            raise FileNotFoundError(path)
        return object()

    def file(self, path, mode):
        self.existing.add(path)
        return FakeFile(self.store, path)

    def chmod(self, path, mode):
        self.chmods.append((path, mode))

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, store=None, existing=None, commands=None):
        self.store = store if store is not None else {}
        self.sftp = FakeSFTP(self.store, existing)
        self.commands = commands or {}
        self.exec_calls = []
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def open_sftp(self):
        return self.sftp

    def exec_command(self, command):
        self.exec_calls.append(command)
        result = self.commands.get(command, (0, ""))
        code, stderr = result
        return None, FakeStream("", code), FakeStream(stderr, code)

    def close(self):
        self.closed = True


def test_format_path_command_and_mode_helpers():
    d = NginxSSHDeployer([])
    assert d._format_path("/etc/nginx/{domain}.pem", "*.example.com") == "/etc/nginx/example.com.pem"
    assert d._build_command(NginxServerConfig("s", "h", sudo=True), " nginx -t ") == "sudo nginx -t"
    assert d._build_command(NginxServerConfig("s", "h", sudo=True), "sudo nginx -t") == "sudo nginx -t"
    assert d._build_command(NginxServerConfig("s", "h"), "nginx -t") == "nginx -t"
    assert d._parse_mode("644") == 0o644
    assert d._parse_mode("") == 0o644
    with pytest.raises(ValueError, match="无效"):
        d._parse_mode("not-octal")


def test_get_ssh_client_key_password_and_missing_auth(monkeypatch, tmp_path):
    fake_client = FakeClient()
    class SSHClientFactory:
        def __call__(self):
            return fake_client
    fake_paramiko = SimpleNamespace(
        SSHClient=SSHClientFactory(),
        AutoAddPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    key = tmp_path / "id_rsa"
    key.write_text("KEY", encoding="utf-8")
    d = NginxSSHDeployer([NginxServerConfig("key", "host", key_path=str(key), username="ops", port=2222)])
    d._get_ssh_client(d._server_map["key"])
    assert fake_client.connect_kwargs["hostname"] == "host"
    assert fake_client.connect_kwargs["port"] == 2222
    assert fake_client.connect_kwargs["username"] == "ops"
    assert fake_client.connect_kwargs["key_filename"] == str(key)

    password_client = FakeClient()
    fake_paramiko.SSHClient = lambda: password_client
    password_d = NginxSSHDeployer([NginxServerConfig("pwd", "host", password="secret")])
    password_d._get_ssh_client(password_d._server_map["pwd"])
    assert password_client.connect_kwargs["password"] == "secret"

    with pytest.raises(ValueError, match="认证需要"):
        NginxSSHDeployer([NginxServerConfig("none", "host")])._get_ssh_client(
            NginxServerConfig("none", "host")
        )

    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError, match="私钥不存在"):
        NginxSSHDeployer([NginxServerConfig("missing", "host", key_path=str(missing))])._get_ssh_client(
            NginxServerConfig("missing", "host", key_path=str(missing))
        )


def test_deploy_success_creates_directory_writes_files_and_reloads(monkeypatch):
    store = {}
    client = FakeClient(store, existing=set(), commands={"nginx -t": (0, ""), "nginx -s reload": (0, "")})
    d = NginxSSHDeployer([NginxServerConfig("web", "host", password="pw")])
    monkeypatch.setattr(d, "_get_ssh_client", lambda server: client)

    result = asyncio.run(
        d.deploy(
            "CERT",
            "KEY",
            DeployTarget("nginx-ssh", "nginx", product_id="web", domain="*.example.com"),
        )
    )
    assert result.success is True
    assert store["/etc/nginx/ssl/example.com/fullchain.pem"] == "CERT"
    assert store["/etc/nginx/ssl/example.com/privkey.pem"] == "KEY"
    assert ("/etc/nginx/ssl/example.com/fullchain.pem", 0o644) in client.sftp.chmods
    assert client.exec_calls[0] == "mkdir -p /etc/nginx/ssl/example.com"
    assert client.closed is True


def test_deploy_success_with_existing_dir_sudo_and_reload_placeholder(monkeypatch):
    client = FakeClient(
        existing={"/ssl/example.com"},
        commands={"sudo nginx -t": (0, ""), "sudo reload example.com": (0, "")},
    )
    server = NginxServerConfig(
        "web", "host", password="pw", sudo=True,
        cert_path="/ssl/{domain}/cert.pem",
        key_path_tmpl="/ssl/{domain}/key.pem",
        reload_command="reload {domain}",
        reload_use_placeholders=True,
    )
    d = NginxSSHDeployer([server])
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="web", domain="example.com")))
    assert result.success
    assert client.exec_calls == ["sudo nginx -t", "sudo reload example.com"]


def test_deploy_failure_paths_and_unknown_server(monkeypatch):
    d = NginxSSHDeployer([NginxServerConfig("web", "host", password="pw")])
    unknown = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="missing")))
    assert unknown.success is False
    assert "未知" in unknown.message

    # mkdir failure
    client = FakeClient(commands={"sudo mkdir -p /etc/nginx/ssl/example.com": (1, "mkdir failed")})
    server = NginxServerConfig("web", "host", password="pw", sudo=True)
    d = NginxSSHDeployer([server])
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="web", domain="example.com")))
    assert result.success is False
    assert "创建目录失败" in result.message

    # nginx test failure
    client = FakeClient(commands={"nginx -t": (1, "syntax error")})
    d = NginxSSHDeployer([NginxServerConfig("web", "host", password="pw")])
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="web", domain="example.com")))
    assert result.success is False
    assert "配置测试失败" in result.message

    # reload failure
    client = FakeClient(commands={"nginx -t": (0, ""), "nginx -s reload": (1, "reload failed")})
    d = NginxSSHDeployer([NginxServerConfig("web", "host", password="pw")])
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="web", domain="example.com")))
    assert result.success is False
    assert "重载失败" in result.message

    # malformed placeholder is caught by deploy
    server = NginxServerConfig("web", "host", password="pw", reload_command="{missing}", reload_use_placeholders=True)
    client = FakeClient(existing={"/etc/nginx/ssl/example.com"})
    d = NginxSSHDeployer([server])
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    result = asyncio.run(d.deploy("CERT", "KEY", DeployTarget("nginx-ssh", "nginx", product_id="web", domain="example.com")))
    assert result.success is False
    assert "占位符" in result.message


def test_list_targets_and_connectivity(monkeypatch):
    servers = [
        NginxServerConfig("one", "10.0.0.1", username="root", password="pw"),
        NginxServerConfig("two", "10.0.0.2", username="ops", password="pw"),
    ]
    d = NginxSSHDeployer(servers)
    targets = asyncio.run(d.list_targets())
    assert [x.product_id for x in targets] == ["one", "two"]
    assert targets[0].metadata["host"] == "10.0.0.1"

    ok = FakeClient(commands={"echo ok": (0, "ignored")})
    # stdout must contain ok; use a dedicated fake for this branch
    class ConnectivityClient(FakeClient):
        def exec_command(self, command):
            self.exec_calls.append(command)
            return None, SimpleNamespace(read=lambda: b"ok"), SimpleNamespace(read=lambda: b"")

    client = ConnectivityClient()
    monkeypatch.setattr(d, "_get_ssh_client", lambda s: client)
    assert asyncio.run(d.check_connectivity("one")) is True
    assert client.closed is True

    monkeypatch.setattr(d, "_get_ssh_client", lambda s: (_ for _ in ()).throw(RuntimeError("down")))
    assert asyncio.run(d.check_connectivity("one")) is False
    assert asyncio.run(d.check_connectivity("missing")) is False
