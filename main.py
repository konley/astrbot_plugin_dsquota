"""DeepSeek 余额查询插件

触发指令: /dsquota
查询 DeepSeek 账户余额，并以信号灯样式的可读文本返回/播报。

可选能力（均默认关闭，可在 WebUI 配置）：
- admin_only：仅 AstrBot 框架管理员可调用 /dsquota
- schedule_enable：按 Cron 表达式定时把余额播报推送给指定的人或群
- monitor_enable：后台轮询，余额跌破阶梯档位时自动告警（防重复打扰）
"""

from datetime import datetime, timezone, timedelta

import httpx

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    _APS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _APS_AVAILABLE = False

TZ = timezone(timedelta(hours=8))

BALANCE_URL = "https://api.deepseek.com/user/balance"
DEFAULT_TITLE = "DeepSeek 余额播报"


def _light(amount: float) -> str:
    """根据余额返回信号灯 emoji：🟢充足 / 🟡偏紧 / 🔴告急。"""
    if amount <= 5:
        return "🔴"
    if amount < 20:
        return "🟡"
    return "🟢"


@register(
    "astrbot_plugin_dsquota",
    "konley",
    "查询 DeepSeek 账户余额，触发指令 /dsquota，支持定时播报与阶梯告警",
    "0.1.0",
)
class DeepSeekQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        # 记录上一次已触发的最低告警档位（元），用于防重复打扰
        self._alerted_level: float | None = None

        if self.config.get("schedule_enable") or self.config.get("monitor_enable"):
            self._setup_scheduler()

    # ---------------- 余额查询 ----------------

    async def _fetch_balance(self) -> dict:
        """请求 DeepSeek 余额接口，返回原始 JSON（失败抛异常）。"""
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("未配置 API Key")

        timeout = self.config.get("timeout", 30) or 30

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.get(BALANCE_URL, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _title(self) -> str:
        return (self.config.get("report_title") or "").strip() or DEFAULT_TITLE

    @staticmethod
    def _parse_balance(data: dict) -> tuple[float, float, float, str] | None:
        """从响应中解析出 (total_balance, granted_balance, topped_up_balance, currency) 或 None。"""
        if not data.get("is_available"):
            return None
        infos = data.get("balance_infos", [])
        if not infos:
            return None
        info = infos[0]
        try:
            total = float(info.get("total_balance", 0))
            granted = float(info.get("granted_balance", 0))
            topped = float(info.get("topped_up_balance", 0))
            currency = info.get("currency", "CNY")
            return total, granted, topped, currency
        except (ValueError, TypeError, IndexError):
            return None

    def _build_message(self, data: dict) -> str:
        """信号灯样式播报：标题 + 余额详情。"""
        parsed = self._parse_balance(data)
        if parsed is None:
            return "查询失败：API 返回异常，请检查 API Key 是否正确。"

        total, granted, topped, currency = parsed

        lines = [f"📊 {self._title()}"]
        lines.append(f"{_light(total)} 总余额：{total:.2f} {currency}")
        lines.append(f"   ├ 赠送余额：{granted:.2f} {currency}")
        lines.append(f"   └ 充值余额：{topped:.2f} {currency}")
        lines.append(f"\n查询时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    # ---------------- 指令入口 ----------------

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为 AstrBot 框架管理员，兼容不同版本 API。"""
        role = getattr(event, "role", None)
        if role is not None:
            return role == "admin"
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    @filter.command("dsquota")
    async def quota(self, event: AstrMessageEvent):
        """查询 DeepSeek 账户余额。"""
        if self.config.get("admin_only") and not self._is_admin(event):
            yield event.plain_result("该指令已设置为仅管理员可用，你没有权限调用。")
            return

        try:
            data = await self._fetch_balance()
        except ValueError:
            yield event.plain_result(
                "未配置 API Key。请在 AstrBot WebUI 的插件配置中填写 DeepSeek API Key 后重试。"
            )
            return
        except httpx.HTTPStatusError as e:
            logger.error(f"[dsquota] HTTP {e.response.status_code}: {e.response.text}")
            yield event.plain_result(
                f"查询失败：服务器返回 {e.response.status_code}，请检查 API Key 是否正确。"
            )
            return
        except httpx.RequestError as e:
            logger.error(f"[dsquota] 请求异常: {e}")
            yield event.plain_result("查询失败：网络请求异常，请稍后重试。")
            return
        except Exception as e:
            logger.error(f"[dsquota] 未知错误: {e}")
            yield event.plain_result("查询失败：发生未知错误，请查看日志。")
            return

        yield event.plain_result(self._build_message(data))

    # ---------------- 主动推送基础设施 ----------------

    def _resolve_platform_id(self) -> str | None:
        """解析推送用的平台实例 ID。

        优先用用户在 WebUI 配置的 schedule_platform 值匹配平台实例 ID；
        若匹配不到，自动查找第一个 aiocqhttp 类型平台实例。
        """
        configured = (self.config.get("schedule_platform") or "").strip()

        pm = getattr(self.context, "platform_manager", None)
        insts = getattr(pm, "platform_insts", None)
        if not insts:
            insts_dict = getattr(pm, "platforms", None)
            if isinstance(insts_dict, dict) and insts_dict:
                if configured and configured in insts_dict:
                    return configured
                return next(iter(insts_dict.keys()), None)
            return configured or None

        if configured:
            for inst in insts:
                try:
                    if inst.meta().id == configured:
                        return configured
                except Exception:
                    pass

        if configured:
            for inst in insts:
                try:
                    ptype = type(inst).__name__
                    if configured.lower() in ptype.lower():
                        return inst.meta().id
                except Exception:
                    pass

        for inst in insts:
            try:
                ptype = type(inst).__name__
                if "aiocqhttp" in ptype.lower():
                    return inst.meta().id
            except Exception:
                pass

        for inst in insts:
            try:
                return inst.meta().id
            except Exception:
                pass
        return configured or None

    def _build_session(self) -> str | None:
        """根据配置（纯数字号码）构造统一会话标识 unified_msg_origin。"""
        target_type = (self.config.get("schedule_target_type") or "group").strip()
        target_id = (self.config.get("schedule_target_id") or "").strip()
        if not target_id:
            return None
        platform = self._resolve_platform_id() or "aiocqhttp"
        msg_type = "GroupMessage" if target_type == "group" else "PrivateMessage"
        return f"{platform}:{msg_type}:{target_id}"

    async def _send(self, text: str):
        """主动发送一条文本到配置的目标会话。"""
        session = self._build_session()
        if not session:
            logger.warning("[dsquota] 未配置推送目标号码，跳过发送。")
            return
        try:
            from astrbot.api.event import MessageChain

            await self.context.send_message(session, MessageChain().message(text))
            logger.info(f"[dsquota] 已推送到 {session}。")
        except Exception as e:
            logger.error(f"[dsquota] 推送发送失败：{e}")

    # ---------------- 调度器 ----------------

    def _setup_scheduler(self):
        """根据配置注册定时播报与监控告警任务。"""
        if not _APS_AVAILABLE:
            logger.error("[dsquota] 未安装 apscheduler，定时/监控功能不可用。")
            return

        self.scheduler = AsyncIOScheduler(timezone=TZ)

        if self.config.get("schedule_enable"):
            cron = (self.config.get("schedule_cron") or "0 9 * * *").strip()
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=TZ)
                self.scheduler.add_job(self._scheduled_push, trigger, id="dsquota_push")
                logger.info(f"[dsquota] 定时播报已启用，Cron='{cron}'。")
            except ValueError as e:
                logger.error(f"[dsquota] Cron 表达式无效：'{cron}'，错误：{e}")

        if self.config.get("monitor_enable"):
            interval = self.config.get("monitor_interval", 10) or 10
            try:
                interval = max(1, int(interval))
            except (ValueError, TypeError):
                interval = 10
            self.scheduler.add_job(
                self._monitor_check,
                IntervalTrigger(minutes=interval, timezone=TZ),
                id="dsquota_monitor",
            )
            logger.info(f"[dsquota] 阶梯告警监控已启用，间隔 {interval} 分钟。")

        if self.scheduler.get_jobs():
            self.scheduler.start()

    async def _scheduled_push(self):
        """定时任务回调：查询并主动播报余额。"""
        try:
            data = await self._fetch_balance()
            text = self._build_message(data)
        except Exception as e:
            logger.error(f"[dsquota] 定时查询失败：{e}")
            text = "定时查询 DeepSeek 余额失败，请检查 API Key 或网络。"
        await self._send(text)

    # ---------------- 阶梯告警核心逻辑 ----------------

    def _parse_thresholds(self) -> list[float]:
        """解析告警阈值配置（元），返回从高到低排序、去重的浮点数列表。

        配置格式逗号分隔，如 "50,20,10,5,1" 表示余额跌破 50→20→10→5→1 时各告警一次。
        """
        raw = (self.config.get("monitor_alarm_thresholds") or "20,10,5,1").strip()
        thresholds = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                thresholds.append(float(part))
            except ValueError:
                continue
        return sorted(set(thresholds), reverse=True)

    @staticmethod
    def _crossed_threshold(balance: float, thresholds: list[float]) -> float | None:
        """返回当前余额已跌破的最高阈值（即应触发告警的档位），未跌破任何阈值返回 None。

        thresholds 已从高到低排序。例如 [50,20,10,5,1]，余额 8 -> 命中 10? 否（8 < 10），
        继续检查 5 -> 8 > 5 不命中 -> 实际命中 10（最低的已跌破阈值）。
        """
        hit = None
        for t in thresholds:
            if balance <= t:
                hit = t
        return hit

    async def _monitor_check(self):
        """后台轮询：检查余额，跨过新档位时告警一次。"""
        thresholds = self._parse_thresholds()
        if not thresholds:
            return
        try:
            data = await self._fetch_balance()
        except Exception as e:
            logger.error(f"[dsquota] 监控查询失败：{e}")
            return

        parsed = self._parse_balance(data)
        if parsed is None:
            return
        balance = parsed[0]  # total_balance

        hit = self._crossed_threshold(balance, thresholds)
        last = self._alerted_level

        if hit is None:
            # 余额已回升到所有阈值之上，重置告警状态
            self._alerted_level = None
            return

        # 仅当跌破了"更低的新档位"时才再次告警
        if last is None or hit < last:
            self._alerted_level = hit
            text = (
                f"⚠️ {self._title()} - 余额告警\n"
                f"🔴 DeepSeek 余额仅剩 {balance:.2f} 元（已跌破 {hit} 元档）\n"
                f"请留意充值。"
            )
            await self._send(text)
        elif hit > last:
            # 余额有所回升但仍在告警区间，更新记录但不重复打扰
            self._alerted_level = hit

    async def terminate(self):
        """插件卸载/重载时停止调度器，避免任务残留。"""
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = None
