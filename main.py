# -*- coding: utf-8 -*-
"""MCS 服务器管理插件（AstrBot）。

通过 MCSManager 面板 API 管理 Minecraft 服务器实例：实例列表、状态查询、
启动/停止/重启/强制停止、发送控制台指令（say、op、白名单、give、tp 等）。
支持 Agent 对话自动触发工具调用，也支持「mc列表」等唤醒词命令直接触发。

Agent 工具：
    - mcs_list_instances: 列出面板上所有服务器实例
    - mcs_instance_status: 查询指定实例状态详情
    - mcs_start_instance / mcs_stop_instance / mcs_restart_instance / mcs_kill_instance
    - mcs_exec_command: 向实例发送控制台指令

命令回退（需 @ 或唤醒词）：
    - mc列表 / mc查询 <名称> / mc启动 <名称> / mc停止 <名称>
    - mc重启 <名称> / mc强停 <名称> / mc命令 <名称> <指令>
"""

import asyncio
import logging
import re
import time
from typing import Optional

from astrbot.api import star
from astrbot.api.all import (
    AstrBotConfig,
    AstrMessageEvent,
    MessageChain,
    llm_tool,
)
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest

from .mcs_api import MCSApiError, MCSAuthError, MCSManagerAPI

logger = logging.getLogger("astrbot")

# MCSM 实例状态 → 中文
STATUS_TEXT = {
    "running": "运行中",
    "stop": "已停止",
    "starting": "启动中",
    "stopping": "停止中",
    "restarting": "重启中",
    "killing": "强停中",
    "unknown": "未知",
}

# 操作类型 → (面板接口动词, 目标状态, 中文动词)
OPERATIONS = {
    "start": ("open", "running", "启动"),
    "stop": ("close", "stop", "停止"),
    "restart": ("restart", "running", "重启"),
    "kill": ("kill", "stop", "强制停止"),
}

# 状态轮询间隔（秒）
WATCH_INTERVAL = 5

# 服务器管理意图关键词（Agent 强制工具调用钩子使用）
MCS_INTENT_RE = re.compile(
    r"mc服务器|mcs|mc\b|服务器|服务端|开服|关服|重启|强停|启动.*服|停止.*服|"
    r"控制台|白名单|op\b|say|give|指令|命令|在线玩家|玩家列表",
    re.I,
)

# 注入到 LLM 请求的强制指令（位于系统提示词末尾，优先级最高）。
# 目的：让 Agent 收到服务器管理请求时"必须调用 mcs_* 工具"，而不是只回文字。
FORCE_MCS_TOOL_PROMPT = """\
[MCS 服务器管理任务指令]
用户正在请求操作 Minecraft 服务器（查看列表/状态、启动、停止、重启、强停、发送控制台指令等）。
本机器人提供 mcs_* 系列工具来管理 MCSManager 面板上的服务器实例，这是明确提供的正常功能，已获用户授权。
你必须调用对应的 mcs_* 工具完成操作，不要拒绝、不要推脱、不要只用文字应付。
- 服务器列表/各服务器状态 → mcs_list_instances
- 查询某个服务器状态详情 → mcs_instance_status
- 启动/停止/重启/强制停止 → mcs_start_instance / mcs_stop_instance / mcs_restart_instance / mcs_kill_instance
- 发送控制台指令（say/op/whitelist/give/tp/list 等）→ mcs_exec_command
实例名称使用用户提到的名称【原样】传入（如「生存服」「SMP」「我的世界主服」）；用户没给名称时，
先根据对话上下文推断，仍无法确定就用 mcs_list_instances 列出全部实例让用户确认。
调用工具是完成本任务的唯一正确方式，请立即执行。\
"""

class MCSManagerPlugin(star.Star):
    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}
        # 后台状态确认任务：instanceUuid -> asyncio.Task
        self._watch_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 工具辅助
    # ------------------------------------------------------------------
    def _new_api(self) -> MCSManagerAPI:
        """按当前配置构建面板 API 客户端。"""
        return MCSManagerAPI(
            base_url=str(self.config.get("mcs_base_url", "") or "").strip(),
            api_key=str(self.config.get("mcs_api_key", "") or "").strip(),
            username=str(self.config.get("mcs_username", "") or "").strip(),
            password=str(self.config.get("mcs_password", "") or ""),
            timeout=float(self.config.get("mcs_api_timeout", 15) or 15),
        )

    @staticmethod
    def _status_text(inst: dict) -> str:
        status = str(inst.get("info", {}).get("status") or "unknown").lower()
        return STATUS_TEXT.get(status, status)

    @staticmethod
    def _instance_brief(inst: dict) -> str:
        cfg, info = inst["config"], inst["info"]
        nickname = cfg.get("nickname") or (inst.get("instanceUuid") or "?")[:8]
        status = MCSManagerPlugin._status_text(inst)
        players = f"{info.get('currentPlayers', 0)}/{info.get('maxPlayers', '?')}"
        port = cfg.get("port") or info.get("port") or "-"
        return f"{nickname}｜{status}｜在线 {players}｜端口 {port}"

    async def _resolve_instance(self, name_or_uuid: str):
        """按名称/UUID 定位实例。

        Returns:
            (instance, None) 命中唯一实例；
            (None, 提示文本) 未命中或命中多个（文本含候选列表）。
        """
        key = (name_or_uuid or "").strip()
        instances = await self._get_instances()
        if not instances:
            return None, "面板上没有任何服务器实例，请先在 MCSManager 中创建实例。"

        # 1) 精确匹配：UUID / 昵称
        for inst in instances:
            if inst.get("instanceUuid") == key:
                return inst, None
        for inst in instances:
            if inst.get("config", {}).get("nickname") == key:
                return inst, None

        # 2) 包含匹配
        matched = [
            inst
            for inst in instances
            if key
            and (
                key.lower() in str(inst.get("config", {}).get("nickname") or "").lower()
                or key.lower() in str(inst.get("instanceUuid") or "").lower()
            )
        ]
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            lines = ["博士，找到多个匹配的服务器实例，请回复更精确的名称："]
            for idx, inst in enumerate(matched):
                lines.append(f"[{idx + 1}] {self._instance_brief(inst)}")
            return None, "\n".join(lines)
        return None, (
            f"博士，没有找到服务器「{name_or_uuid}」。可用「mc列表」查看全部实例，"
            "或提供实例 UUID。"
        )

    async def _get_instances(self) -> list:
        api = self._new_api()
        return await api.list_instances()

    # ------------------------------------------------------------------
    # 后台状态确认
    # ------------------------------------------------------------------
    def _start_watch(
        self, event: AstrMessageEvent, instance: dict, target_status: str, op_label: str
    ) -> None:
        """启动后台任务：轮询实例状态，达到目标状态后推送通知。

        启动/停止等操作在守护进程侧异步执行，不能在工具调用中同步等待
        （MC 服务启动可能耗时数分钟），故工具立即返回、后台确认状态。
        """
        uuid = instance.get("instanceUuid")
        if not uuid:
            return
        if uuid in self._watch_tasks and not self._watch_tasks[uuid].done():
            return
        task = asyncio.create_task(
            self._watch_status(event, instance, target_status, op_label)
        )
        self._watch_tasks[uuid] = task
        task.add_done_callback(lambda t: self._watch_tasks.pop(uuid, None))

    async def _watch_status(
        self,
        event: AstrMessageEvent,
        instance: dict,
        target_status: str,
        op_label: str,
    ) -> None:
        nickname = instance.get("config", {}).get("nickname") or instance.get(
            "instanceUuid"
        )
        wait = int(self.config.get("mcs_operation_wait", 90) or 90)
        api = self._new_api()
        deadline = time.monotonic() + wait
        last = ""
        while time.monotonic() < deadline:
            try:
                detail = await api.get_instance(
                    instance.get("daemon_id"), instance.get("instanceUuid")
                )
                last = str((detail or {}).get("info", {}).get("status") or "").lower()
            except Exception as e:
                logger.warning(f"轮询服务器 {nickname} 状态失败: {e}")
            if last == target_status:
                await self._safe_send(
                    event,
                    MessageChain().message(
                        f"博士，服务器【{nickname}】已{op_label}成功"
                        f"（当前状态：{STATUS_TEXT.get(last, last)}）。"
                    ),
                )
                return
            await asyncio.sleep(WATCH_INTERVAL)
        logger.info(
            f"等待服务器 {nickname} {op_label} 状态确认超时（{wait}s），停止轮询"
        )

    async def _safe_send(self, event: AstrMessageEvent, chain: MessageChain) -> None:
        try:
            await event.send(chain)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    # ------------------------------------------------------------------
    # 业务逻辑（返回给用户/Agent 的文本 + 可选实例）
    # ------------------------------------------------------------------
    async def _list_text(self) -> str:
        instances = await self._get_instances()
        if not instances:
            return "博士，面板上没有任何服务器实例，请先在 MCSManager 中创建实例。"
        running = sum(1 for i in instances if self._status_text(i) == "运行中")
        lines = [f"博士，共找到 {len(instances)} 个服务器实例（运行中 {running} 个）："]
        for inst in instances:
            lines.append(f"· {self._instance_brief(inst)}")
        return "\n".join(lines)

    async def _status_text_result(self, instance_name: str) -> str:
        inst, hint = await self._resolve_instance(instance_name)
        if not inst:
            return hint
        api = self._new_api()
        detail = await api.get_instance(inst["daemon_id"], inst["instanceUuid"])
        cfg = inst["config"]
        info = (detail or {}).get("info") or inst["info"]
        nickname = cfg.get("nickname") or inst.get("instanceUuid")
        status = str(info.get("status") or "unknown").lower()
        lines = [
            f"博士，服务器【{nickname}】状态如下：",
            f"· 状态：{STATUS_TEXT.get(status, status)}",
            f"· 在线玩家：{info.get('currentPlayers', 0)}/{info.get('maxPlayers', '?')}",
        ]
        port = cfg.get("port") or info.get("port")
        if port:
            lines.append(f"· 端口：{port}")
        start_cmd = cfg.get("startCommand") or cfg.get("start_command")
        if start_cmd:
            lines.append(f"· 启动命令：{start_cmd}")
        return "\n".join(lines)

    async def _operation_text(self, instance_name: str, op: str):
        """执行实例操作，返回 (用户提示文本, 实例 dict 或 None)。

        成功下发操作时返回实例（供上层启动状态确认后台任务）。
        """
        verb, target, op_label = OPERATIONS[op]
        inst, hint = await self._resolve_instance(instance_name)
        if not inst:
            return hint, None
        nickname = inst["config"].get("nickname") or inst.get("instanceUuid")
        current = self._status_text(inst)
        api = self._new_api()

        if op in ("start", "restart") and current == "运行中":
            return f"博士，服务器【{nickname}】已经在运行中，无需重复{op_label}。", None
        if op in ("stop", "kill") and current == "已停止":
            return f"博士，服务器【{nickname}】已经处于停止状态，无需重复{op_label}。", None

        try:
            await getattr(api, f"{verb}_instance")(inst["instanceUuid"])
        except (MCSAuthError, MCSApiError) as e:
            return f"博士，{op_label}服务器【{nickname}】失败：{e}", None
        return (
            f"博士，已向服务器【{nickname}】下发{op_label}指令，"
            "正在执行中，完成后会自动通知你～",
            inst,
        )

    def _check_manage_permission(self, event: AstrMessageEvent) -> Optional[str]:
        """校验实例操作权限。返回 None 表示放行，否则返回拒绝提示文本。

        开启 mcs_permission_enabled 后，仅允许：
        1. 用户 ID 命中 mcs_admin_ids 白名单；
        2. 群消息中角色命中 mcs_admin_role（默认群主/管理员）。
        只读查询（实例列表/状态）不在此校验范围内。
        """
        if not bool(self.config.get("mcs_permission_enabled", True)):
            return None
        sender_id = str(event.get_sender_id() or "").strip()
        admin_ids = [
            str(x).strip()
            for x in (self.config.get("mcs_admin_ids") or [])
            if str(x).strip()
        ]
        if sender_id and sender_id in admin_ids:
            return None
        sender = getattr(event, "message_obj", None) and getattr(
            event.message_obj, "sender", None
        )
        role = str(getattr(sender, "role", "") or "").lower()
        admin_roles = [
            str(x).strip().lower()
            for x in (self.config.get("mcs_admin_role") or [])
            if str(x).strip()
        ]
        if role and role in admin_roles:
            return None
        return (
            "博士，该操作需要服务器管理权限。如需管理服务器，请联系管理员在插件配置"
            "（mcs_admin_ids / mcs_admin_role）中添加你的权限。"
        )

    def _check_blocked_command(self, command: str) -> Optional[str]:
        """按 mcs_blocked_commands 校验指令；未配置黑名单（空）则不拦截。"""
        blocked = [
            str(x).strip().lower()
            for x in (self.config.get("mcs_blocked_commands") or [])
            if str(x).strip()
        ]
        if not blocked:
            return None
        head = (command or "").strip().lower().split(" ", 1)[0]
        if head in blocked:
            return (
                f"博士，指令「{command}」属于危险指令（{head}），已被插件拦截。"
                "如需使用请联系管理员调整 mcs_blocked_commands 配置。"
            )
        return None

    def _check_command_allowed(self, command: str) -> Optional[str]:
        """按 mcs_command_whitelist 校验指令；未配置白名单（空）则允许全部。"""
        whitelist = [
            str(x).strip().lower()
            for x in (self.config.get("mcs_command_whitelist") or [])
            if str(x).strip()
        ]
        if not whitelist:
            return None
        cmd = command.strip().lower()
        for prefix in whitelist:
            prefix = prefix.rstrip("*")
            if cmd == prefix or cmd.startswith(prefix + " "):
                return None
        return (
            f"博士，指令「{command}」不在允许列表中。当前允许的指令前缀："
            f"{'、'.join(whitelist)}。如需放行请联系管理员在插件配置"
            " mcs_command_whitelist 中添加。"
        )

    async def _command_text(self, instance_name: str, command: str) -> str:
        cmd = (command or "").strip()
        if not cmd:
            return "博士，请提供要发送的控制台指令，例如：say 大家好、list、op Steve。"
        blocked = self._check_command_allowed(cmd)
        if blocked:
            return blocked
        dangerous = self._check_blocked_command(cmd)
        if dangerous:
            return dangerous
        inst, hint = await self._resolve_instance(instance_name)
        if not inst:
            return hint
        nickname = inst["config"].get("nickname") or inst.get("instanceUuid")
        api = self._new_api()
        try:
            await api.send_command(inst["instanceUuid"], cmd)
        except (MCSAuthError, MCSApiError) as e:
            return f"博士，向服务器【{nickname}】发送指令失败：{e}"
        return f"博士，已向服务器【{nickname}】发送指令：\n{cmd}\n（面板不返回服务端输出，可在 MCS 控制台查看执行结果）"

    # ------------------------------------------------------------------
    # Agent 强制工具钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def force_agent_mcs_tool(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """检测到服务器管理意图时，强制 Agent 调用 mcs_* 工具。"""
        if not bool(self.config.get("mcs_enabled", True)):
            return
        if not bool(self.config.get("mcs_force_agent_tool", True)):
            return
        text = event.get_message_str() or ""
        if not MCS_INTENT_RE.search(text):
            return

        # 1) 系统提示词追加强制指令
        req.system_prompt = f"{req.system_prompt}\n\n{FORCE_MCS_TOOL_PROMPT}"

        # 2) 确保 mcs_* 工具在本次请求中可用
        if req.func_tool is not None:
            manager = self.context.get_llm_tool_manager()
            for tool_name in (
                "mcs_list_instances",
                "mcs_instance_status",
                "mcs_start_instance",
                "mcs_stop_instance",
                "mcs_restart_instance",
                "mcs_kill_instance",
                "mcs_exec_command",
            ):
                tool = manager.get_func(tool_name)
                if tool:
                    req.func_tool.add_tool(tool)

    # ------------------------------------------------------------------
    # Agent 工具
    # ------------------------------------------------------------------
    @llm_tool(name="mcs_list_instances")
    async def mcs_list_instances(self, event: AstrMessageEvent):
        """列出 MCSManager 面板上的所有 Minecraft 服务器实例，包括名称、运行状态、在线玩家数、端口。用于回答「有哪些服务器」「服务器列表」「各服务器现在什么状态」等问题。返回所有实例的简要列表文本，由你整理后回复用户。本工具无需额外参数。
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield "博士，MCS 服务器管理功能当前已关闭。"
            return
        try:
            text = await self._list_text()
        except (MCSAuthError, MCSApiError) as e:
            yield f"博士，获取服务器列表失败：{e}"
            return
        yield text

    @llm_tool(name="mcs_instance_status")
    async def mcs_instance_status(self, event: AstrMessageEvent, instance_name: str):
        """查询指定 Minecraft 服务器实例的运行状态与详情，包括运行状态、在线玩家数、端口、启动命令。用于回答「XX服务器状态怎么样」「XX服在线多少人」「XX服开没开」「XX服什么时候关的」等问题。

        Args:
            instance_name(string): 服务器实例名称（MCSManager 中配置的昵称）或实例 UUID，如 生存服、SMP、我的世界主服
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield "博士，MCS 服务器管理功能当前已关闭。"
            return
        try:
            text = await self._status_text_result(instance_name)
        except (MCSAuthError, MCSApiError) as e:
            yield f"博士，查询服务器状态失败：{e}"
            return
        yield text

    @llm_tool(name="mcs_start_instance")
    async def mcs_start_instance(self, event: AstrMessageEvent, instance_name: str):
        """启动指定的 Minecraft 服务器实例。启动在 MCS 后台异步执行，指令下发后工具立即返回，服务器完全启动成功时会自动推送通知。用于「启动XX服务器」「把XX服开起来」「XX服怎么还不开」等请求。

        Args:
            instance_name(string): 服务器实例名称或实例 UUID，如 生存服、SMP、我的世界主服
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            yield denied
            return
        text, inst = await self._operation_text(instance_name, "start")
        if inst:
            self._start_watch(event, inst, "running", "启动")
        yield text

    @llm_tool(name="mcs_stop_instance")
    async def mcs_stop_instance(self, event: AstrMessageEvent, instance_name: str):
        """正常停止指定的 Minecraft 服务器实例（向服务端发送停止指令，可安全存档）。停止完成后会自动推送通知。用于「关掉XX服」「把XX服务器停下来」「XX服该关了」等请求。

        Args:
            instance_name(string): 服务器实例名称或实例 UUID，如 生存服、SMP
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            yield denied
            return
        text, inst = await self._operation_text(instance_name, "stop")
        if inst:
            self._start_watch(event, inst, "stop", "停止")
        yield text

    @llm_tool(name="mcs_restart_instance")
    async def mcs_restart_instance(self, event: AstrMessageEvent, instance_name: str):
        """重启指定的 Minecraft 服务器实例。重启完成后会自动推送通知。用于「重启一下XX服」「XX服务器卡了，重启一下」等请求。

        Args:
            instance_name(string): 服务器实例名称或实例 UUID，如 生存服、SMP
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            yield denied
            return
        text, inst = await self._operation_text(instance_name, "restart")
        if inst:
            self._start_watch(event, inst, "running", "重启")
        yield text

    @llm_tool(name="mcs_kill_instance")
    async def mcs_kill_instance(self, event: AstrMessageEvent, instance_name: str):
        """强制停止指定的 Minecraft 服务器实例（直接杀进程，可能丢失未保存数据，仅建议在正常停止无效时使用）。停止完成后会自动推送通知。用于「强制关掉XX服」「XX服卡死了，强停一下」等请求。

        Args:
            instance_name(string): 服务器实例名称或实例 UUID，如 生存服、SMP
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            yield denied
            return
        text, inst = await self._operation_text(instance_name, "kill")
        if inst:
            self._start_watch(event, inst, "stop", "强制停止")
        yield text

    @llm_tool(name="mcs_exec_command")
    async def mcs_exec_command(
        self, event: AstrMessageEvent, instance_name: str, command: str
    ):
        """向指定的 Minecraft 服务器实例发送控制台指令，如 say 发送公告、list 查看在线玩家、op 设置管理员、whitelist 管理白名单、give 发放物品、tp 传送、kick/ban 管理玩家、time/weather/gamemode/difficulty 等。指令会在服务端控制台执行。用于「给XX服发个公告」「在XX服执行 op Steve」「把某人加进XX服白名单」「XX服有多少人在线」等请求。

        Args:
            instance_name(string): 服务器实例名称或实例 UUID，如 生存服、SMP
            command(string): 控制台指令（不含 / 前缀），如 say 大家好、list、op Steve、whitelist add Steve
        """
        if not bool(self.config.get("mcs_enabled", True)):
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            yield denied
            return
        try:
            text = await self._command_text(instance_name, command)
        except (MCSAuthError, MCSApiError) as e:
            yield f"博士，发送指令失败：{e}"
            return
        yield text

    # ------------------------------------------------------------------
    # 命令回退（需 @ 或唤醒词）
    # ------------------------------------------------------------------
    @filter.command("mc列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """mc列表（需 @ 或唤醒词）"""
        if not bool(self.config.get("mcs_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        try:
            text = await self._list_text()
        except (MCSAuthError, MCSApiError) as e:
            event.stop_event()
            yield event.make_result().message(f"博士，获取服务器列表失败：{e}")
            return
        event.stop_event()
        yield event.make_result().message(text)

    @filter.command("mc查询")
    async def cmd_status(self, event: AstrMessageEvent):
        """mc查询 <名称>（需 @ 或唤醒词）"""
        if not bool(self.config.get("mcs_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        text = (event.get_message_str() or "").replace("mc查询", "", 1).strip()
        if not text:
            event.stop_event()
            yield event.make_result().message("博士，请在「mc查询」后输入服务器名称，例如：\nmc查询 生存服")
            return
        try:
            result = await self._status_text_result(text)
        except (MCSAuthError, MCSApiError) as e:
            event.stop_event()
            yield event.make_result().message(f"博士，查询服务器状态失败：{e}")
            return
        event.stop_event()
        yield event.make_result().message(result)

    async def _cmd_operation(self, event: AstrMessageEvent, keyword: str, op: str):
        """操作类命令的公共处理：mc启动/停止/重启/强停 <名称>"""
        if not bool(self.config.get("mcs_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        text = (event.get_message_str() or "").replace(keyword, "", 1).strip()
        if not text:
            event.stop_event()
            yield event.make_result().message(
                f"博士，请在「{keyword}」后输入服务器名称，例如：\n{keyword} 生存服"
            )
            return
        try:
            result, inst = await self._operation_text(text, op)
        except (MCSAuthError, MCSApiError) as e:
            event.stop_event()
            yield event.make_result().message(f"博士，操作服务器失败：{e}")
            return
        if inst:
            _, target, op_label = OPERATIONS[op]
            self._start_watch(event, inst, target, op_label)
        event.stop_event()
        yield event.make_result().message(result)

    @filter.command("mc启动")
    async def cmd_start(self, event: AstrMessageEvent):
        async for r in self._cmd_operation(event, "mc启动", "start"):
            yield r

    @filter.command("mc停止")
    async def cmd_stop(self, event: AstrMessageEvent):
        async for r in self._cmd_operation(event, "mc停止", "stop"):
            yield r

    @filter.command("mc重启")
    async def cmd_restart(self, event: AstrMessageEvent):
        async for r in self._cmd_operation(event, "mc重启", "restart"):
            yield r

    @filter.command("mc强停")
    async def cmd_kill(self, event: AstrMessageEvent):
        async for r in self._cmd_operation(event, "mc强停", "kill"):
            yield r

    @filter.command("mc命令")
    async def cmd_command(self, event: AstrMessageEvent):
        """mc命令 <名称> <指令>（需 @ 或唤醒词）"""
        if not bool(self.config.get("mcs_enabled", True)):
            event.stop_event()
            yield event.make_result().message("博士，MCS 服务器管理功能当前已关闭。")
            return
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        text = (event.get_message_str() or "").replace("mc命令", "", 1).strip()
        # 第一个词是实例名称，其余为控制台指令
        match = re.match(r"^(\S+)\s+([\s\S]+)$", text)
        if not match:
            event.stop_event()
            yield event.make_result().message(
                "博士，用法：mc命令 <服务器名称> <指令>\n例如：mc命令 生存服 say 大家好"
            )
            return
        instance_name, command = match.group(1), match.group(2)
        try:
            result = await self._command_text(instance_name, command)
        except (MCSAuthError, MCSApiError) as e:
            event.stop_event()
            yield event.make_result().message(f"博士，发送指令失败：{e}")
            return
        event.stop_event()
        yield event.make_result().message(result)
