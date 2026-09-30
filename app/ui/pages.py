# -*- coding: utf-8 -*-
import re
from pathlib import Path

from nicegui import ui, Client

from app import state
from app import config as app_config
from app.config import get_config
from app.services.collector import collect_all
from app.services.renew_flow import (
    issue_and_plan_deploy,
    renew_and_plan_deploy,
    format_provider_type_summary,
    deploy_existing_only,
)
from app.services.config_runtime import apply_config_text
from app.services.renew import prepare_issue_domains
from app.utils.domain_match import normalize_domain
from app.utils.time import TimeUtil
from app.utils.wrap import prevent_double_click


_HOME_USAGE_TOOLTIP = (
    "托管域名列表：点「签发」编辑主域名、通配域名及其他 SAN。\n"
    "证书池已有证书：点「强制续签」按钮重新签发。\n"
    "已签发或续签但部署失败：点「部署」按钮重新部署。"
)


def _parse_issue_domain_text(value: str) -> list[str]:
    """解析签发弹窗中的换行、空格或逗号分隔域名。"""
    raw_domains = [item for item in re.split(r"[\s,，]+", value or "") if item]
    if not raw_domains:
        raise ValueError("请至少填写一个签发域名")
    return prepare_issue_domains(raw_domains[0], raw_domains)


def _default_issue_main_domain(managed_domain: str, selected_domain: str) -> str:
    """根据所点主机名生成默认主域名，且不越出所属 DNS 托管域名。"""
    managed = normalize_domain(managed_domain)
    selected = normalize_domain(selected_domain)
    if not managed or managed.startswith(".") or ".." in managed or "*" in managed:
        raise ValueError(f"托管域名无效: {managed_domain}")

    selected_is_wildcard = selected.startswith("*.")
    selected_base = selected[2:] if selected_is_wildcard else selected
    if (
        not selected_base
        or selected_base.startswith(".")
        or ".." in selected_base
        or "*" in selected_base
    ):
        raise ValueError(f"所选域名无效: {selected_domain}")

    if selected_base == managed:
        return managed
    if not selected_base.endswith(f".{managed}"):
        raise ValueError(
            f"所选域名不属于托管域名: selected={selected_base} managed={managed}"
        )

    # 通配记录已经明确了证书作用域，直接使用去掉 *. 后的域名。
    if selected_is_wildcard:
        return selected_base

    # 去掉所点主机名最左侧一级，例如 oss.test.example.com -> test.example.com。
    return selected_base.split(".", 1)[1]


def _notify_safe(client, message: str, **kwargs) -> None:
    """
    在 await 等耗时逻辑之后调用 ui.notify。
    若 Client 已被回收（用户关闭/刷新页面），NiceGUI 会从 Client.instances 中
    移除该 client.id，此时直接静默跳过，避免触发 "Client has been deleted" 告警。
    """
    if client.id not in Client.instances:
        return
    with client:
        ui.notify(message, **kwargs)


def _load_config_text() -> str:
    path = Path(app_config.CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return path.read_text(encoding="utf-8")


def _format_time_shanghai(raw, fallback: str = "N/A") -> str:
    if raw in (None, ""):
        return fallback
    if str(raw).strip().lower() in ("unknow", "unknown"):
        return "unknown"
    try:
        dt_utc = TimeUtil.parse(raw)
        return TimeUtil.to_local_tz(dt_utc, "Asia/Shanghai")
    except Exception:
        return str(raw)


def _format_last_updated_shanghai(raw) -> str:
    return _format_time_shanghai(raw, fallback="N/A")

# ===============================
# 证书大盘 - 现代极简表格
# ===============================
def render_certificates():
    config = get_config()
    ui.label('证书池 (Certificates)').classes('text-2xl font-medium text-gray-800 dark:text-gray-100 tracking-tight')

    async def _manual_deploy_handler(args: str) -> None:
        domain = args.strip()
        if not domain:
            raise ValueError("手动部署失败: domain 为空")

        client = ui.context.client
        deploy_mode = get_config().deploy.mode.strip().lower()
        cfg = get_config()
        if cfg.staging.enabled and getattr(cfg.staging, "use_self_signed", True):
            source_label = "本地调试(certs)"
        else:
            source_label = "线上(acme.sh)"
        ui.notify(
            f"开始仅部署证书 {domain}（来源={source_label}, mode={deploy_mode}）",
            type="info",
            position="top-right",
            timeout=8000,
            close_button=True,
        )

        try:
            result = await deploy_existing_only(
                domain=domain,
                provider_name=None,
            )
        except Exception as exc:
            _notify_safe(
                client,
                f"仅部署失败: {exc}",
                type="negative",
                multi_line=True,
                position="top-right",
                timeout=12000,
                close_button=True,
            )
            return

        summary_text = format_provider_type_summary(result.get("summary"))
        success_count = int(result.get("success_count", 0) or 0)
        failed_count = int(result.get("failed_count", 0) or 0)
        planned = len(result.get("planned_bind_actions", []))

        if result.get("mode") == "apply":
            _notify_safe(
                client,
                f"仅部署完成: success={success_count} failed={failed_count} | {summary_text}",
                type="positive",
                position="top-right",
                timeout=8000,
                close_button=True,
            )
            return

        _notify_safe(
            client,
            f"仅部署 dry-run 计划生成完成: 命中 {planned} 个资源 | {summary_text}",
            type="positive",
            position="top-right",
            timeout=8000,
            close_button=True,
        )

    async def _manual_force_renew_handler(args: str) -> None:
        domain = args.strip()
        if not domain:
            raise ValueError("手动续签失败: domain 为空")

        client = ui.context.client
        deploy_mode = get_config().deploy.mode.strip().lower()
        ui.notify(
            f"开始强制续签（忽略冷却）{domain}，mode={deploy_mode}",
            type="info",
            position="top-right",
            timeout=8000,
            close_button=True,
        )

        result = await renew_and_plan_deploy(
            domain=domain,
            provider_name=None,
            force=True,
        )

        renew_status = str(result.get("renew_status", "unknown"))
        retry_count = int(result.get("retry_count", 0) or 0)
        summary_text = format_provider_type_summary(result.get("summary"))

        with client:
            if renew_status == "skipped":
                cooldown_days = result.get("cooldown_days")
                tip = f"最近 {cooldown_days} 天内已成功续签，本次请求已跳过" if cooldown_days else "最近已成功续签，本次请求已跳过"
                ui.notify(
                    tip,
                    type="info",
                    position="top-right",
                    timeout=8000,
                    close_button=True,
                )
                return

            if renew_status in ("deploy_failed", "deploy_failed_pending"):
                error = result.get("error", "部署失败，等待人工处理")
                ui.notify(
                    f"证书已续签，但部署失败并已停止自动处理: {error}",
                    type="negative",
                    multi_line=True,
                    position="top-right",
                    timeout=12000,
                    close_button=True,
                )
                return

            if renew_status == "failed":
                error = result.get("error", "续签失败")
                ui.notify(
                    f"续签失败（重试 {retry_count} 次）: {error}，详细原因请查看钉钉告警",
                    type="negative",
                    multi_line=True,
                    position="top-right",
                    timeout=12000,
                    close_button=True,
                )
                return

            # 续签成功场景，继续展示部署结果摘要
            if result.get("mode") == "apply":
                ui.notify(
                    f"续签成功（重试 {retry_count} 次），apply 完成: success={result.get('success_count', 0)} failed={result.get('failed_count', 0)} | {summary_text}",
                    type="positive",
                    position="top-right",
                    timeout=8000,
                    close_button=True,
                )
                return
            ui.notify(
                f"续签成功（重试 {retry_count} 次），dry-run 计划生成完成: 命中 {len(result.get('planned_bind_actions', []))} 个资源 | {summary_text}",
                type="positive",
                position="top-right",
                timeout=8000,
                close_button=True,
            )

    columns = [
        {'name': 'domain', 'label': '证书主域名', 'field': 'domain', 'align': 'left', 'sortable': True},
        {'name': 'key', 'label': 'KeyLength', 'field': 'key', 'align': 'left'},
        {'name': 'sans', 'label': 'SAN 域名', 'align': 'left'},
        {'name': 'ca', 'label': 'CA', 'field': 'ca', 'align': 'left'},
        {'name': 'exp', 'label': '过期时间', 'field': 'exp', 'align': 'left', 'sortable': False},
        {'name': 'days', 'label': '剩余天数', 'field': 'days', 'align': 'left', 'sortable': True},
        {'name': 'provider', 'label': '所属', 'field': 'provider', 'align': 'left', 'sortable': True},
        {'name': 'action', 'label': '操作', 'align': 'left'},
    ]

    with ui.table(columns=columns, rows=state.CERT_LIST, row_key='domain').classes(
        'w-full mt-3 shadow-none border border-gray-200 dark:border-gray-800 rounded-lg text-base'
    ).props('flat bordered dense binary-state-sort') as table:
        async def _on_manual_renew_force(e) -> None:
            await _manual_force_renew_handler(e.args)

        async def _on_manual_deploy(e) -> None:
            await _manual_deploy_handler(e.args)

        table.on('manual-renew-force', _on_manual_renew_force)
        table.on('manual-deploy', _on_manual_deploy)
        
        # 证书主域名列
        table.add_slot('body-cell-domain', '''
            <q-td :props="props" class="text-left">
                <div class="flex items-center gap-2 text-base">
                    <q-icon name="lock" size="sm" :class="props.row.days <= 15 ? 'text-red-500' : 'text-gray-400'" />
                    <span class="font-mono text-gray-800 dark:text-gray-200">{{ props.row.domain }}</span>
                </div>
            </q-td>
        ''')

        # SAN 域名多行展示
        table.add_slot('body-cell-sans', '''
            <q-td :props="props" class="text-left align-top">
                <div v-for="san in props.row.sans" :key="san" class="text-sm font-mono text-gray-500 break-all leading-tight mb-0.5">
                    {{ san }}
                </div>
            </q-td>
        ''')

        # 剩余天数状态徽章
        cert_w = config.alert.cert.cert_warn_days
        cert_e = config.alert.cert.cert_expiry_days
        table.add_slot('body-cell-days', f'''
            <q-td :props="props" class="text-left">
                <q-chip dense :color="props.row.days <= {cert_w} ? 'red-1' : props.row.days <= {cert_e} ? 'orange-1' : 'green-1'" 
                        :text-color="props.row.days <= {cert_w} ? 'red-7' : props.row.days <= {cert_e} ? 'orange-8' : 'green-8'"
                        class="px-3 py-1 text-sm font-medium tracking-wide">
                    {{{{ props.row.days }}}}
                </q-chip>
            </q-td>
        ''')
        
        # 操作列：强制续签 / 仅部署
        table.add_slot('body-cell-action', '''
            <q-td :props="props" class="text-left">
                <div class="flex items-center gap-3">
                    <q-btn
                        flat
                        size="md"
                        label="强制续签"
                        color="orange"
                        @click="$parent.$emit('manual-renew-force', props.row.domain)"
                    >
                        <q-tooltip>忽略冷却窗口，强制续签并部署</q-tooltip>
                    </q-btn>
                    <q-btn
                        flat
                        size="md"
                        label="部署"
                        color="secondary"
                        @click="$parent.$emit('manual-deploy', props.row.domain)"
                    >
                        <q-tooltip>不续签，只使用当前证书执行多云部署</q-tooltip>
                    </q-btn>
                </div>
            </q-td>
        ''')

# ===============================
# 域名管理 - 折叠列表式
# ===============================
def render_domains():
    config = get_config()
    ui.label('托管域名 (Managed Domains)').classes('text-2xl font-medium text-gray-800 dark:text-gray-100 tracking-tight mt-10 mb-3')

    def _open_issue_dialog(managed_domain: str, provider_name: str, selected_domain: str) -> None:
        client = ui.context.client
        default_main_domain = _default_issue_main_domain(
            managed_domain,
            selected_domain,
        )
        default_domains = f"{default_main_domain}\n*.{default_main_domain}"

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label(f'签发证书：{selected_domain}').classes('text-xl font-medium')
            ui.label(
                f'证书主域名取第一项，当前默认使用 {default_main_domain}；可增加域名或删除通配域名。'
            ).classes('text-sm text-gray-500')
            domains_input = ui.textarea(
                label='签发域名（每行一个，也支持空格或逗号分隔）',
                value=default_domains,
            ).props('outlined autogrow').classes('w-full font-mono')

            @prevent_double_click
            async def _confirm_issue() -> None:
                try:
                    issue_domains = _parse_issue_domain_text(domains_input.value or '')
                except ValueError as exc:
                    ui.notify(str(exc), type='negative', position='top-right')
                    return

                dialog.close()
                deploy_mode = get_config().deploy.mode.strip().lower()
                ui.notify(
                    f"开始签发 {issue_domains[0]}，域名数={len(issue_domains)}（mode={deploy_mode}）",
                    type='info',
                    position='top-right',
                    timeout=8000,
                    close_button=True,
                )
                result = await issue_and_plan_deploy(
                    domain=issue_domains[0],
                    domains=issue_domains,
                    provider_name=provider_name or None,
                )
                issue_status = str(result.get('issue_status', 'unknown'))
                summary_text = format_provider_type_summary(result.get('summary'))

                if issue_status == 'failed':
                    _notify_safe(
                        client,
                        f"签发失败: {result.get('error', '未知错误')}，已有证书请使用“强制续签”",
                        type='negative',
                        multi_line=True,
                        position='top-right',
                        timeout=12000,
                        close_button=True,
                    )
                    return
                if issue_status == 'deploy_failed':
                    _notify_safe(
                        client,
                        f"证书已签发，但部署失败: {result.get('error', '未知错误')}，可使用“部署”重试",
                        type='negative',
                        multi_line=True,
                        position='top-right',
                        timeout=12000,
                        close_button=True,
                    )
                    return
                if result.get('mode') == 'apply':
                    message = (
                        f"签发成功，apply 完成: success={result.get('success_count', 0)} "
                        f"failed={result.get('failed_count', 0)} | {summary_text}"
                    )
                else:
                    message = (
                        "签发成功，dry-run 计划生成完成: "
                        f"命中 {len(result.get('planned_bind_actions', []))} 个资源 | {summary_text}"
                    )
                _notify_safe(
                    client,
                    message,
                    type='positive',
                    position='top-right',
                    timeout=8000,
                    close_button=True,
                )

            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('取消', on_click=dialog.close).props('flat')
                ui.button('确认签发', on_click=_confirm_issue).props('color=primary unelevated')

        dialog.open()

    # 外层容器：无缝边框
    with ui.column().classes('w-full gap-0 border border-gray-200 dark:border-gray-800 rounded-lg overflow-hidden bg-white dark:bg-gray-900'):
        for i, group in enumerate(state.DOMAIN_GROUPS):
            if i > 0:
                ui.separator().classes('bg-gray-100 dark:bg-gray-800')

            # 单行展开项
            with ui.expansion().classes('w-full').props('expand-separator') as exp:
                
                # 展开项 Header 定制
                with exp.add_slot('header'):
                    header_grid = 'display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: center; width: 100%; padding: 8px 0;'
                    with ui.element('div').style(header_grid):
                        with ui.row().classes('items-center gap-3 flex-nowrap'):
                            # 状态点
                            has_expiry = str(group.get('expires_at') or '').lower() not in ('', 'unknown', 'unknow')
                            warn_days = has_expiry and group['days'] <= config.alert.domain.domain_warn_days
                            exp_days = has_expiry and group['days'] <= config.alert.domain.domain_expiry_days
                            
                            is_danger = warn_days
                            is_warn = exp_days
                            dot_color = 'bg-gray-400' if not has_expiry else ('bg-red-500' if is_danger else ('bg-orange-400' if is_warn else 'bg-green-500'))
                            ui.element('div').classes(f'w-2.5 h-2.5 rounded-full {dot_color} shadow-sm shrink-0')
                            ui.label(group['domain']).classes('text-lg font-mono font-medium text-gray-800 dark:text-gray-100 truncate')
                            issue_button = ui.button(
                                '签发',
                                on_click=lambda root=group['domain'], provider=(group.get('dns_name') or group.get('name', '')): _open_issue_dialog(
                                    root, provider, root
                                ),
                            ).props('flat dense color=primary').tooltip('编辑域名并首次签发')
                            if not (group.get('dns_name') or group.get('name')):
                                issue_button.disable()
                                issue_button.tooltip('未发现 DNS 托管 Provider，不能在线签发')
                        
                        ui.label(f"过期: {_format_time_shanghai(group.get('expires_at', 'unknown'), fallback='unknown')}").classes('text-base text-gray-600 dark:text-gray-400 truncate')
                        ui.label(group.get('registrant_org') or '注册主体未知').classes('text-base text-gray-600 dark:text-gray-400 truncate')
                        ui.label(
                            f"注册: {group.get('registrar_provider') or group.get('provider') or '未知'} / {group.get('registrar_name') or group.get('name') or '未知'}"
                        ).classes('text-sm text-gray-600 dark:text-gray-400 truncate')
                        ui.label(
                            f"DNS: {group.get('dns_provider') or group.get('provider') or '未知'} / {group.get('dns_name') or group.get('name') or '未知'}"
                        ).classes('text-sm font-medium text-blue-600 dark:text-blue-400 truncate')
                
                # 展开后的子域名内容（内嵌一个极简的「子表格」）
                with ui.column().classes('w-full bg-gray-50/50 dark:bg-gray-900/50 px-6 py-4 border-t border-gray-100 dark:border-gray-800'):
                    
                    # 强网格布局，确保表头和数据行完全对齐
                    grid_style = 'display: grid; grid-template-columns: minmax(0, 4fr) minmax(0, 2fr) minmax(0, 3fr) 180px; gap: 16px; align-items: center; width: 100%;'

                    # 子表头
                    with ui.element('div').style(grid_style).classes('text-sm text-gray-400 font-medium px-2 mb-3 tracking-wider'):
                        ui.label('SUBDOMAIN')
                        ui.label('STATUS')
                        ui.label('REMARK')
                        ui.label('ACTIONS').classes('text-right')

                    # 子行
                    for sub in group['subs']:
                        with ui.element('div').style(grid_style).classes('text-base px-2 py-3 hover:bg-white dark:hover:bg-gray-800 rounded-md transition-all'):
                            ui.label(sub['name']).classes('font-mono text-gray-700 dark:text-gray-300 break-all')
                            
                            # 状态徽章
                            status_str = str(sub['status'])
                            if '在工作' in status_str or 'ok' in status_str.lower() or '正常' in status_str:
                                status_class = 'text-green-700 bg-green-100/80 border-green-200'
                                icon = 'check_circle'
                            elif '异常' in status_str or '错误' in status_str or 'fail' in status_str.lower():
                                status_class = 'text-red-700 bg-red-100/80 border-red-200'
                                icon = 'error_outline'
                            else:
                                status_class = 'text-gray-600 bg-gray-100 border-gray-200'
                                icon = 'remove_circle_outline'
                            
                            with ui.element('div').classes('flex items-center gap-1.5'):
                                ui.icon(icon, size='14px').classes(status_class.split()[0])
                                ui.label(status_str).classes(f'text-xs px-2 py-0.5 rounded-md border inline-block {status_class}')
                            
                            ui.label(sub.get('remark', '')).classes('text-gray-500 text-sm truncate')
                            
                            # 操作列：多级子域默认使用所点主机名的直接父域作为证书主域名。
                            default_issue_main = _default_issue_main_domain(
                                group['domain'],
                                sub['name'],
                            )
                            with ui.row().classes('gap-2 justify-end flex-nowrap'):
                                ui.button(
                                    '签发',
                                    on_click=lambda root=group['domain'], provider=(group.get('dns_name') or group.get('name', '')), selected=sub['name']: _open_issue_dialog(
                                        root, provider, selected
                                    ),
                                ).props('flat dense color=primary').tooltip(
                                    f"默认签发 {default_issue_main} 与 *.{default_issue_main}"
                                )


# ===============================
# 页面入口
# ===============================
def render_top_nav(active: str) -> None:
    with ui.row().classes('w-full items-center justify-between mb-4'):
        with ui.row().classes('gap-2'):
            ui.button(
                "大盘",
                on_click=lambda: ui.navigate.to("/"),
            ).props("unelevated color=primary" if active == "dashboard" else "outline")
            ui.button(
                "配置中心",
                on_click=lambda: ui.navigate.to("/config"),
            ).props("unelevated color=primary" if active == "config" else "outline")


def build_ui():
    @prevent_double_click
    async def on_collect_now() -> None:
        client = ui.context.client
        ui.notify("开始手动采集...", type="info")
        try:
            await collect_all()
        except Exception as exc:
            _notify_safe(client, "手动采集失败: %s" % exc, type="negative", multi_line=True)
            return
        _notify_safe(client, "手动采集完成，正在刷新页面", type="positive")
        if client.id in Client.instances:
            await client.run_javascript("window.location.reload()")

    # 改为屏幕的极致宽度，充分利用横向空间
    with ui.column().classes('w-full max-w-screen-2xl mx-auto py-8 px-4 sm:px-6 lg:px-8'):
        render_top_nav("dashboard")
        
        # 极简页面头
        with ui.row().classes('w-full items-end justify-between mb-6'):
            with ui.column().classes('gap-2'):
                ui.label('CertOps Dashboard').classes('text-4xl font-semibold tracking-tight text-gray-900 dark:text-white')
                ui.label('自动化证书续签与域名状态平台').classes('text-base text-gray-500')
            with ui.column().classes('items-end gap-2'):
                with ui.row().classes('items-center gap-1'):
                    ui.label('使用说明').classes('text-sm text-gray-500')
                    with ui.icon('help_outline', size='sm').classes(
                        'text-gray-400 cursor-help'
                    ):
                        ui.tooltip(_HOME_USAGE_TOOLTIP).style(
                            'white-space: pre-line; max-width: 320px; line-height: 1.6;'
                        )
                ui.label(f"上次采集(上海): {_format_last_updated_shanghai(state.LAST_UPDATED)}").classes('text-sm text-gray-500')
                ui.button("立即采集", on_click=on_collect_now).props("color=primary unelevated")
        
        render_certificates()
        render_domains()


def build_config_ui():
    config_text = _load_config_text()

    with ui.column().classes('w-full max-w-screen-2xl mx-auto py-8 px-4 sm:px-6 lg:px-8 gap-4'):
        render_top_nav("config")
        with ui.row().classes('w-full items-end justify-between'):
            with ui.column().classes('gap-1'):
                ui.label('配置中心 (Config Center)').classes('text-3xl font-semibold tracking-tight text-gray-900 dark:text-white')
                ui.label('全量编辑 config.yaml，点击保存后自动强校验并热生效（不触发立即采集）').classes('text-base text-gray-500')

        editor = ui.textarea(label="config.yaml（全量编辑）", value=config_text).props("autogrow outlined")
        editor.classes('w-full')
        editor.style('min-height: 560px;')

        status_label = ui.label("状态：待保存").classes("text-sm text-gray-500")
        error_label = ui.label("").classes("text-sm text-red-600 whitespace-pre-wrap")

        async def on_save() -> None:
            client = ui.context.client
            status_label.set_text("状态：校验中并应用配置...")
            error_label.set_text("")
            try:
                await apply_config_text(editor.value or "")
            except Exception as exc:
                detail = str(exc)
                status_label.set_text("状态：保存失败")
                error_label.set_text(detail)
                _notify_safe(client, "配置保存失败，请按错误提示修改", type="negative", multi_line=True)
                return

            editor.value = _load_config_text()
            status_label.set_text("状态：保存成功，配置已热生效（采集需手动触发或等待调度）")
            _notify_safe(client, "配置保存成功，已热生效", type="positive")

        with ui.row().classes("gap-2 mt-2"):
            ui.button("保存配置", on_click=on_save).props("color=primary unelevated")
