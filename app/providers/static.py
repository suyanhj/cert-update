from typing import List
from .base import Provider
import asyncio

class StaticCloudProvider(Provider):
    """使用配置中静态域名列表，适合测试和初始化环境。"""

    domain_roles = frozenset({"registration", "dns"})

    async def is_domain_provider(self, domain: str) -> bool:
        """判断是否是当前提供器负责处理。"""
        for item in self.config.static_domains or []:
            if domain == item.domain:
                return True
        return False


    def _full_domain(self):
        static_domain_names = []
        for item in self.config.static_domains:
            for name in item.sub_domains:
                if name == '@':
                    static_domain_names.append(item.domain)
                else:
                    static_domain_names.append(f'{name}.{item.domain}')
        return static_domain_names


    async def get_domain_list(self) -> List[dict]:
        """获取账号下的所有域名列表。"""
        domains: List[dict] = []
        static_domains = self.config.static_domains
        static_domain_names: List[str] = []

        self.logger.info("%s 静态发现 %d 个域名", self.name, len(static_domains))
        self.logger.info("%s 静态发现 %d 条 DNS 记录", self.name, len(self._full_domain()))
        self.logger.info("%s 开始探测", self.name)

        for item in static_domains:
            # 只取当前主域下的全部子域名
            for name in item.sub_domains:
                static_domain_names.append(super()._full_domain(item.domain, name))

            domain_name = item.domain
            expires_at = item.expires_at
            if expires_at in ("unknown", "unknow"):
                days = 0
            else:
                days = self.time.remaining_days(self.time.parse(expires_at))
            subs = await self.discover_sub_domains(static_domain_names)

            _ = {
                "domain": domain_name,
                "name": self.name,
                "expires_at": expires_at,
                "days": days,
                "org": item.registrant_org,
                "provider": self.config.type,
                "subs": subs,
            }
            domains.append(_)
            static_domain_names.clear()

        self.logger.info("%s 探测完成", self.name)
        return domains


    async def discover_sub_domains(self, domains) -> list[dict]:
        sem = asyncio.Semaphore(self.config.concurrency or 20)

        tasks = [
            self._probe_domain(domain, sem=sem)
            for domain in domains
        ]

        return await asyncio.gather(*tasks)
