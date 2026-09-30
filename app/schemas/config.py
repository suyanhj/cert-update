import pathlib
from email.utils import parseaddr
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings


class AcmeConfig(BaseModel):
    """ACME 配置。"""

    cert_storage_path: Union[pathlib.Path, str] = Field(
        "./certs",
        description="证书存储目录",
    )
    acme_sh_path: Optional[str] = Field(
        "acme.sh",
        description="acme.sh 可执行文件路径",
    )
    certificate_path_mode: Literal["auto", "default", "custom"] = Field(
        "auto",
        description="证书路径模式：自动识别、acme.sh 默认目录或 install-cert 自定义路径",
    )
    command_timeout_seconds: int = Field(
        900,
        gt=0,
        description="单次 acme.sh 命令最长执行时间（秒）",
    )
    dns_sleep_seconds: int = Field(
        60,
        ge=0,
        description="续签时传给 acme.sh 的 --dnssleep（秒），等待 DNS TXT 记录传播；0 表示不传",
    )


class DingDingConfig(BaseModel):
    """钉钉通知配置。"""

    webhook: HttpUrl = Field(..., description="钉钉机器人 Webhook 地址")
    secret: Optional[str] = Field(None, description="加签密钥，为空则不加签")
    enabled: bool = Field(True, description="是否启用钉钉通知；额度用完后可关闭并改走 Rocket.Chat")


class RocketChatConfig(BaseModel):
    """Rocket.Chat Incoming Webhook 通知配置。"""

    webhook: HttpUrl = Field(..., description="Rocket.Chat Incoming Webhook 地址")
    channel: Union[str, List[str]] = Field(..., description="频道，单个字符串或多个字符串列表")
    username: str = Field(..., description="消息显示的用户名")
    icon_emoji: str = Field(..., description="消息图标 emoji")
    title: str = Field(..., description="attachment 标题")
    enabled: bool = Field(True, description="是否启用 Rocket.Chat 通知")

    @field_validator("channel")
    @classmethod
    def channel_not_empty(cls, value: Union[str, List[str]]) -> Union[str, List[str]]:
        """channel 必须是非空字符串，或多个非空字符串。"""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("rocketchat.channel 不能为空")
            return value
        if not isinstance(value, list) or not value:
            raise ValueError("rocketchat.channel 必须是字符串或非空列表")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("rocketchat.channel 列表项必须是非空字符串")
        return value


class EmailConfig(BaseModel):
    """SMTP 邮件通知配置。"""

    enabled: bool = Field(True, description="是否启用邮件通知")
    host: str = Field("", description="SMTP 服务器地址")
    port: int = Field(587, ge=1, le=65535, description="SMTP 服务器端口")
    security: Literal["starttls", "ssl", "none"] = Field(
        "starttls",
        description="连接安全模式",
    )
    username: Optional[str] = Field(None, description="SMTP 登录用户名；为空时不认证")
    password: Optional[str] = Field(None, description="SMTP 密码，建议改用 password_env")
    password_env: Optional[str] = Field(None, description="SMTP 密码环境变量名，优先于 password")
    from_address: str = Field("", description="发件人地址")
    to: List[str] = Field(default_factory=list, description="主收件人列表")
    cc: List[str] = Field(default_factory=list, description="抄送列表")
    subject_prefix: str = Field("[CRT] ", description="邮件主题前缀")
    timeout_seconds: float = Field(10.0, gt=0, description="SMTP 连接与发送超时秒数")

    @staticmethod
    def _normalize_address(value: Any, field_name: str) -> str:
        text = str(value or "").strip()
        if not text or "\r" in text or "\n" in text:
            raise ValueError(f"{field_name} 必须是有效邮件地址")
        _, address = parseaddr(text)
        if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
            raise ValueError(f"{field_name} 必须是有效邮件地址")
        return text

    @field_validator("host", "username", "password_env", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        if value is None:
            return None
        return str(value).strip()

    @field_validator("from_address", mode="before")
    @classmethod
    def validate_from_address(cls, value):
        if value in (None, ""):
            return ""
        return cls._normalize_address(value, "email.from_address")

    @field_validator("to", "cc", mode="before")
    @classmethod
    def normalize_recipients(cls, value, info):
        values = [value] if isinstance(value, str) else value
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError(f"email.{info.field_name} 必须是字符串或列表")
        return [
            cls._normalize_address(item, f"email.{info.field_name}")
            for item in values
        ]

    @field_validator("subject_prefix")
    @classmethod
    def validate_subject_prefix(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("email.subject_prefix 不能包含换行")
        return value

    @model_validator(mode="after")
    def validate_auth(self):
        if not self.enabled:
            return self
        if not self.host:
            raise ValueError("email.host 不能为空")
        if not self.from_address:
            raise ValueError("email.from_address 不能为空")
        if not self.to:
            raise ValueError("email.to 至少需要一个收件人")
        if self.username and not (self.password or self.password_env):
            raise ValueError("email.username 启用认证时必须配置 password 或 password_env")
        return self


class StaticDomainConfig(BaseModel):
    """静态域名配置，用于没有 SDK 发现能力时的手工录入。"""

    domain: str
    expires_at: Union[str, bool] = "unknown"
    registrant_org: Optional[str] = None
    sub_domains: Union[List[str], Dict[str, Any]] = Field(default_factory=list)


class SubExtension(BaseModel):
    """子域扩展配置。"""

    domain: str
    redirect_to: str


class ProviderConfig(BaseModel):
    """通用云平台配置模型。"""

    name: str
    type: Literal[
        "tencent",
        "aliyun",
        "huawei",
        "volcengine",
        "qiniu",
        "cloudflare",
        "custom",
    ]
    enabled: bool = True
    check_extensions: bool = Field(False, description="是否检查泛域名扩展")
    credentials: Dict[str, str] = Field(default_factory=dict)
    static_domains: List[StaticDomainConfig] = Field(default_factory=list)
    sub_extensions: List[SubExtension] = Field(default_factory=list)


class CloudProductScanConfig(BaseModel):
    """单个云厂商的产品扫描开关。"""

    cdn_scan: bool = Field(True, description="是否扫描 CDN 产品")
    live_scan: bool = Field(False, description="是否扫描云直播播放域名 SSL 绑定（当前仅腾讯云）")
    eo_scan: bool = Field(False, description="是否扫描 EdgeOne 域名 SSL 绑定（当前仅腾讯云）")
    oss_scan: bool = Field(True, description="是否扫描 OSS/对象存储 相关产品")
    lb_scan: bool = Field(True, description="是否扫描负载均衡类产品（SLB/ALB/ELB 等）")
    waf_scan: bool = Field(False, description="是否扫描 WAF 防护域名证书绑定")
    ecs_scan: bool = Field(True, description="是否扫描云主机/ECS 实例")
    domain_scan: bool = Field(True, description="是否扫描云上域名信息")


class NginxSSHAuthConfig(BaseModel):
    """Nginx SSH 认证配置。"""

    type: Literal["key", "password"] = Field("key", description="认证方式")
    key_path: Optional[str] = Field(None, description="私钥路径（type=key 时必填）")
    password: Optional[str] = Field(None, description="密码（type=password 时可填）")
    password_env: Optional[str] = Field(None, description="密码环境变量名（优先于 password）")
    passphrase_env: Optional[str] = Field(None, description="私钥口令环境变量名")


class NginxSSHProfileConfig(BaseModel):
    """SSH Profile，可被多个主机复用。"""

    name: str = Field(..., description="Profile 名称")
    username: str = Field("root", description="SSH 用户名")
    auth: NginxSSHAuthConfig = Field(default_factory=NginxSSHAuthConfig)
    connect_timeout_seconds: int = Field(10, description="连接超时秒数")


class NginxHostConfig(BaseModel):
    """Nginx 主机配置。"""

    name: str = Field(..., description="主机标识")
    host: str = Field(..., description="主机地址")
    port: int = Field(22, description="SSH 端口")
    ssh_profile: str = Field(..., description="引用 ssh_profiles.name")
    sudo: bool = Field(False, description="远端命令是否通过 sudo 执行")
    base_dir: str = Field("/etc/nginx/ssl", description="证书目录基础路径")
    test_command: str = Field("nginx -t", description="配置检测命令")
    reload_command: str = Field("nginx -s reload", description="重载命令")
    backup_dir: Optional[str] = Field("/var/backups/certs", description="备份目录")
    timeout_seconds: int = Field(20, description="单主机部署超时秒数")
    reload_use_placeholders: bool = Field(
        False,
        description="是否对 reload_command 启用占位符格式化（仅支持 {domain}）",
    )


class NginxCertLayoutConfig(BaseModel):
    """证书文件布局。"""

    cert_file: str = Field(
        "{base_dir}/{resolved_cert_name}/fullchain.pem",
        description=(
            "证书文件路径模板；支持 base_dir、resolved_cert_name、main_domain、cert_name，"
            "其中 resolved_cert_name 为 cert_name_template 的渲染结果"
        ),
    )
    key_file: str = Field(
        "{base_dir}/{resolved_cert_name}/privkey.pem",
        description=(
            "私钥文件路径模板；支持 base_dir、resolved_cert_name、main_domain、cert_name，"
            "其中 resolved_cert_name 为 cert_name_template 的渲染结果"
        ),
    )
    owner: str = Field("root", description="文件 owner")
    group: str = Field("root", description="文件 group")
    mode: str = Field("644", description="证书与私钥文件权限（八进制字符串，如 644）")


class NginxTargetGroupConfig(BaseModel):
    """Nginx 目标组（一个证书可部署到多个主机）。"""

    name: str = Field(..., description="目标组名称")
    hosts: List[str] = Field(default_factory=list, description="引用 nginx_hosts.name 列表")
    cert_layout: NginxCertLayoutConfig = Field(default_factory=NginxCertLayoutConfig)


class NginxDeployRuleConfig(BaseModel):
    """证书到目标组的部署规则。"""

    cert_domains: List[str] = Field(
        default_factory=list,
        description="匹配证书的域名/证书文件名主体列表（主域名或 SAN，可带 *. 前缀）",
    )
    target_group: str = Field(..., description="引用 nginx_target_groups.name")
    cert_name_template: str = Field("{main_domain}", description="证书目录命名模板")


class NginxConfig(BaseModel):
    """Nginx 部署配置。"""

    nginx_hosts: List[NginxHostConfig] = Field(default_factory=list, description="Nginx 主机列表")
    nginx_target_groups: List[NginxTargetGroupConfig] = Field(
        default_factory=list,
        description="Nginx 目标组列表",
    )
    nginx_deploy_rules: List[NginxDeployRuleConfig] = Field(
        default_factory=list,
        description="Nginx 部署规则列表",
    )


class BindingCacheConfig(BaseModel):
    """云产品绑定缓存配置。"""

    enabled: bool = Field(True, description="是否启用证书产品映射缓存")
    ttl_seconds: int = Field(3600, description="缓存有效期（秒）")
    verify_before_apply: bool = Field(True, description="apply 前是否对命中目标做实时校验")


class DeployConfig(BaseModel):
    """部署配置。"""

    mode: Literal["dry-run", "apply"] = Field(
        "dry-run",
        description="部署模式：dry-run 只输出计划，apply 执行实际更新",
    )
    binding_cache: BindingCacheConfig = Field(
        default_factory=BindingCacheConfig,
        description="证书产品映射缓存配置",
    )
    renew_cooldown_days: int = Field(
        7,
        description="证书续签成功后的冷却天数，在该时间窗口内再次续签会被跳过（force=true 时忽略）",
    )
    max_renew_retries: int = Field(
        5,
        description="单次续签触发内允许的最大重试次数（包含首次尝试）",
    )
    renew_retry_interval_seconds: int = Field(
        0,
        description="证书续签失败时两次重试之间的等待时间（秒，<=0 表示不等待）",
    )


class CertAlertConfig(BaseModel):
    enabled: bool = True
    cert_warn_days: int = 15
    cert_expiry_days: int = 28


class DomainAlertConfig(BaseModel):
    enabled: bool = True
    domain_warn_days: int = 15
    domain_expiry_days: int = 28


class EcsAlertConfig(BaseModel):
    enabled: bool = True
    ecs_warn_days: int = 15
    ecs_expiry_days: int = 28


class AlertConfig(BaseModel):
    """告警配置。"""

    cert: CertAlertConfig = Field(default_factory=CertAlertConfig)
    domain: DomainAlertConfig = Field(default_factory=DomainAlertConfig)
    ecs: EcsAlertConfig = Field(default_factory=EcsAlertConfig)


class StagingConfig(BaseModel):
    """Staging 环境特定配置。"""

    enabled: bool = Field(False, description="是否启用 staging 模式")
    use_self_signed: bool = Field(True, description="staging 下是否用自签证书模拟续签（true=本地 cryptography 自签，false=仍走 acme.sh）")


class GlobalProductScanConfig(BaseModel):
    """全局云产品扫描开关，按云厂商 + 产品类型控制。"""

    aliyun: CloudProductScanConfig = Field(
        default_factory=CloudProductScanConfig,
        description="阿里云产品扫描开关",
    )
    huawei: CloudProductScanConfig = Field(
        default_factory=CloudProductScanConfig,
        description="华为云产品扫描开关",
    )
    tencent: CloudProductScanConfig = Field(
        default_factory=CloudProductScanConfig,
        description="腾讯云产品扫描开关",
    )
    volcengine: CloudProductScanConfig = Field(
        default_factory=CloudProductScanConfig,
        description="火山引擎产品扫描开关",
    )
    qiniu: CloudProductScanConfig = Field(
        default_factory=CloudProductScanConfig,
        description="七牛云产品扫描开关",
    )
    cloudflare: CloudProductScanConfig = Field(
        default_factory=lambda: CloudProductScanConfig(
            cdn_scan=False,
            live_scan=False,
            eo_scan=False,
            oss_scan=False,
            lb_scan=False,
            waf_scan=False,
            ecs_scan=False,
            domain_scan=True,
        ),
        description="Cloudflare DNS 产品扫描开关",
    )


class AppConfig(BaseSettings):
    """应用配置入口，可通过 .env、环境变量或 YAML 填充。"""

    model_config = ConfigDict(env_file=".env", extra="allow")

    acme: AcmeConfig
    dingding: Optional[DingDingConfig] = None
    rocketchat: Optional[RocketChatConfig] = None
    email: Optional[EmailConfig] = None
    providers: List[ProviderConfig] = Field(default_factory=list)
    ssh_profiles: List[NginxSSHProfileConfig] = Field(
        default_factory=list,
        description="全局 SSH Profile 列表（供各类主机复用）",
    )
    nginx: NginxConfig = Field(default_factory=NginxConfig, description="Nginx 部署主配置")
    alert: AlertConfig = Field(default_factory=AlertConfig, description="告警配置")
    whitelist: List[str] = Field(
        default_factory=list,
        description="顶层白名单：命中后跳过证书/域名过期告警",
    )
    domain_dns_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description="根域名到 DNS Provider 配置名称的人工覆盖映射",
    )
    deploy: DeployConfig = Field(default_factory=DeployConfig, description="部署配置")
    staging: StagingConfig = Field(default_factory=StagingConfig, description="Staging 环境配置")
    product_scan: GlobalProductScanConfig = Field(
        default_factory=GlobalProductScanConfig,
        description="全局云产品扫描开关（阿里/华为/腾讯/火山/七牛/Cloudflare）",
    )
    auto_renew_enabled: bool = Field(
        True,
        description="是否启用自动续签编排（采集后按 auto_renew_days 阈值触发续签，并按 deploy.mode 执行 dry-run/apply）",
    )
    auto_renew_days: int = Field(20, description="证书剩余多少天时自动触发续签")
    check_interval_minutes: int = Field(10, description="轮询云平台的频率（分钟）")
    log_level: str = Field("info", description="全局日志级别：debug / info / warning / error")
    tls_scan_parallel: int = Field(10, description="TLS 探测并发数")
    http_scan_parallel: int = Field(10, description="HTTP 探测并发数（DNS 子域探测等）")
    tls_timeout_seconds: int = Field(5, description="TLS 探测超时时间（秒）")
    http_timeout_seconds: int = Field(2, description="HTTP 探测超时时间（秒）")

    @field_validator("domain_dns_overrides", mode="before")
    @classmethod
    def normalize_domain_dns_overrides(cls, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError("domain_dns_overrides 必须是对象")
        result: Dict[str, str] = {}
        for raw_domain, raw_provider in value.items():
            domain = str(raw_domain or "").strip().lower().rstrip(".")
            provider_name = str(raw_provider or "").strip()
            if not domain or not provider_name:
                raise ValueError("domain_dns_overrides 的域名和 Provider 名称不能为空")
            result[domain] = provider_name
        return result
