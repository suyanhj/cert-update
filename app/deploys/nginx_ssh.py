"""Nginx SSH 证书部署器。"""

from __future__ import annotations

from app.utils.logger import get_logger
from app.utils.project_root import resolve_project_path
import os
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import List, Optional

from .base import CertificateDeployer, DeployResult, DeployTarget

LOGGER = get_logger("deployer")


@dataclass
class NginxServerConfig:
    """Nginx 服务器配置。"""

    name: str  # 服务器名称/标识
    host: str  # SSH 主机地址
    port: int = 22  # SSH 端口
    username: str = "root"  # SSH 用户名
    key_path: Optional[str] = None  # SSH 私钥路径
    password: Optional[str] = None  # SSH 密码（不推荐）
    sudo: bool = False  # 是否通过 sudo 执行远端命令
    backup_dir: Optional[str] = None  # 证书备份目录
    timeout_seconds: int = 20  # SSH 连接超时时间
    cert_path: str = "/etc/nginx/ssl/{domain}/fullchain.pem"  # 证书存放路径模板
    key_path_tmpl: str = "/etc/nginx/ssl/{domain}/privkey.pem"  # 私钥存放路径模板
    file_mode: str = "644"  # 证书与私钥文件权限（八进制字符串）
    reload_command: str = "nginx -s reload"  # 重载命令
    reload_use_placeholders: bool = False  # 是否对 reload_command 启用占位符格式化（仅支持 {domain})
    test_command: str = "nginx -t"  # 配置测试命令


class NginxSSHDeployer(CertificateDeployer):
    """通过 SSH 部署证书到 Nginx 服务器。"""

    def __init__(self, servers: List[NginxServerConfig]) -> None:
        self._servers = servers
        self._server_map = {s.name: s for s in servers}

    @property
    def name(self) -> str:
        return "nginx-ssh"

    def _get_ssh_client(self, server: NginxServerConfig):
        """获取 SSH 客户端连接。"""
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("paramiko 未安装，请运行: pip install paramiko")
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        connect_kwargs = {
            "hostname": server.host,
            "port": server.port,
            "username": server.username,
            "timeout": server.timeout_seconds,
        }
        
        if server.key_path:
            # 使用私钥认证；相对路径按 crt 项目根解析，与启动目录无关
            key_path = Path(server.key_path).expanduser()
            if not key_path.is_absolute():
                key_path = resolve_project_path(key_path)
            if not key_path.exists():
                raise FileNotFoundError(f"SSH 私钥不存在: {key_path}")
            connect_kwargs["key_filename"] = str(key_path)
        elif server.password:
            # 使用密码认证
            connect_kwargs["password"] = server.password
        else:
            raise ValueError("SSH 认证需要提供私钥路径或密码")
        
        client.connect(**connect_kwargs)
        return client

    def _format_path(self, template: str, domain: str) -> str:
        """格式化路径模板。"""
        # 移除通配符前缀用于文件路径
        safe_domain = domain.lstrip("*.")
        return template.format(domain=safe_domain)

    def _build_command(self, server: NginxServerConfig, command: str) -> str:
        """根据配置构造远端命令（处理 sudo 前缀）。"""
        cmd = command.strip()
        if server.sudo and not cmd.startswith("sudo "):
            return f"sudo {cmd}"
        return cmd

    @staticmethod
    def _parse_mode(mode_str: str) -> int:
        """将八进制权限字符串转为整数，如 '644' -> 0o644。"""
        s = str(mode_str or "").strip()
        if not s:
            return 0o644
        try:
            return int(s, 8)
        except ValueError as e:
            raise ValueError(f"无效的文件权限字符串: {mode_str!r}") from e

    async def deploy(
        self,
        cert_pem: str,
        key_pem: str,
        target: DeployTarget,
        cert_id: Optional[str] = None,
    ) -> DeployResult:
        """通过 SSH 部署证书到 Nginx 服务器。cert_id 本部署器不使用。"""
        server_name = target.product_id
        if not server_name or server_name not in self._server_map:
            return DeployResult(
                success=False,
                target=target,
                message=f"未知的 Nginx 服务器: {server_name}",
            )
        
        server = self._server_map[server_name]
        domain = target.domain or "default"
        
        cert_path = self._format_path(server.cert_path, domain)
        key_path = self._format_path(server.key_path_tmpl, domain)
        
        try:
            client = self._get_ssh_client(server)
            sftp = client.open_sftp()
            
            try:
                # 创建目录（必要时加 sudo）
                cert_dir = os.path.dirname(cert_path)
                try:
                    sftp.stat(cert_dir)
                except FileNotFoundError:
                    # 递归创建目录
                    mkdir_cmd = self._build_command(server, f"mkdir -p {cert_dir}")
                    stdin, stdout, stderr = client.exec_command(mkdir_cmd)
                    exit_code = stdout.channel.recv_exit_status()
                    if exit_code != 0:
                        error = stderr.read().decode()
                        raise RuntimeError(f"创建目录失败: {error}")
                
                # 写入证书文件并按配置设置权限
                with sftp.file(cert_path, "w") as f:
                    f.write(cert_pem)
                sftp.chmod(cert_path, self._parse_mode(server.file_mode))
                LOGGER.info("写入证书: %s@%s:%s (mode=%s)", server.username, server.host, cert_path, server.file_mode)

                # 写入私钥文件并按配置设置权限
                with sftp.file(key_path, "w") as f:
                    f.write(key_pem)
                sftp.chmod(key_path, self._parse_mode(server.file_mode))
                LOGGER.info("写入私钥: %s@%s:%s (mode=%s)", server.username, server.host, key_path, server.file_mode)
                
            finally:
                sftp.close()
            
            # 测试 Nginx 配置
            test_cmd = self._build_command(server, server.test_command)
            stdin, stdout, stderr = client.exec_command(test_cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                error = stderr.read().decode()
                LOGGER.error("Nginx 配置测试失败: cmd=%s error=%s", test_cmd, error)
                return DeployResult(
                    success=False,
                    target=target,
                    message=f"Nginx 配置测试失败: {test_cmd} -> {error}",
                )
            
            # 重载 Nginx
            reload_cmd_raw = server.reload_command
            if server.reload_use_placeholders:
                try:
                    reload_cmd_raw = reload_cmd_raw.format(domain=domain)
                except Exception as e:
                    raise RuntimeError(f"reload_command 占位符格式化失败: {e}") from e
            reload_cmd = self._build_command(server, reload_cmd_raw)
            stdin, stdout, stderr = client.exec_command(reload_cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                error = stderr.read().decode()
                LOGGER.error("Nginx 重载失败: cmd=%s error=%s", reload_cmd, error)
                return DeployResult(
                    success=False,
                    target=target,
                    message=f"Nginx 重载失败: {reload_cmd} -> {error}",
                )
            
            client.close()
            
            LOGGER.info(
                "Nginx SSH 证书部署成功: %s (%s)",
                server.name, domain
            )
            return DeployResult(
                success=True,
                target=target,
                message=f"Nginx 证书部署成功: {server.name} ({domain})",
            )
            
        except Exception as e:
            LOGGER.error("SSH 部署失败: %s", e)
            return DeployResult(
                success=False,
                target=target,
                message=str(e),
            )

    async def list_targets(self) -> List[DeployTarget]:
        """列出所有配置的 Nginx 服务器。"""
        targets: List[DeployTarget] = []
        
        for server in self._servers:
            targets.append(
                DeployTarget(
                    provider="nginx-ssh",
                    product_type="nginx",
                    product_id=server.name,
                    metadata={
                        "host": server.host,
                        "port": server.port,
                        "username": server.username,
                    },
                )
            )
        
        return targets

    async def check_connectivity(self, server_name: str) -> bool:
        """检查与 Nginx 服务器的 SSH 连接。"""
        if server_name not in self._server_map:
            return False
        
        server = self._server_map[server_name]
        
        try:
            client = self._get_ssh_client(server)
            stdin, stdout, stderr = client.exec_command("echo ok")
            result = stdout.read().decode().strip()
            client.close()
            return result == "ok"
        except Exception as e:
            LOGGER.warning("SSH 连接检查失败 %s: %s", server_name, e)
            return False

