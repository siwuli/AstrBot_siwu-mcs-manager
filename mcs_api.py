# -*- coding: utf-8 -*-
"""MCSManager 面板 API 客户端（v10 兼容）。

MCSM v10 认证方式：
1. API Key：以 URL 查询参数 `apikey=<key>` 附加到每个请求（推荐，长期有效）。
2. 账号密码：`POST /api/auth/login` 返回 token 字符串，以 URL 参数 `token=` 附加。

所有请求必须携带请求头 `X-Requested-With: XMLHttpRequest`（面板 CSRF 防护），
否则返回 `[Forbidden] 无法找到请求头 x-requested-with`。

MCSM v10 响应统一为 `{"status": 200, "data": ..., "message": ...}`，status 为数字；
v9 的 status 为字符串 `"ok"/"error"`。本客户端两者兼容。
"""

import logging

import aiohttp

logger = logging.getLogger("astrbot")


class MCSApiError(Exception):
    """MCSManager API 调用失败。"""


class MCSAuthError(MCSApiError):
    """认证失败（凭据缺失 / 登录被拒 / 无权限）。"""


class MCSManagerAPI:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        username: str = "",
        password: str = "",
        timeout: float = 15.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = timeout
        self._token = None
        self._auth_mode = ""  # "" / "apikey" / "token" / "session"
        self._authed = False
        self._session = None

    # ------------------------------------------------------------------ 认证
    async def ensure_auth(self) -> None:
        """确保已具备认证凭据。

        认证策略（v10 优先）：
        1. 配置了 mcs_api_key 时，以 `apikey=<key>` 参数探测一个面板接口；
           v10 面板接受该方式（v9 不支持，会返回 [Forbidden] 权限不足）。
        2. 探测失败或未配置 key 时，改用账号密码登录：
           - v10：`POST /api/auth/login` 的 data 为 token 字符串，用 URL 参数 `token=`；
           - v9：登录后通过 session cookie 维持会话。
        """
        if self._authed:
            return
        if self.api_key:
            try:
                await self._raw_request(
                    "GET", "/api/service/remote_services",
                    params={"apikey": self.api_key},
                )
                self._auth_mode = "apikey"
                self._authed = True
                logger.info("MCS 面板使用 API Key 认证")
                return
            except MCSApiError:
                # 面板拒绝 API Key（常见于 MCSM v9），回退账号密码登录
                pass
        if self.username and self.password:
            data = await self._raw_request(
                "POST", "/api/auth/login",
                json={"username": self.username, "password": self.password},
            )
            # v10 登录成功 data 直接是 token 字符串；v9 可能返回 {"token": ...}
            token = None
            if isinstance(data, str) and data:
                token = data
            elif isinstance(data, dict):
                token = data.get("token") or data.get("key")
            self._auth_mode = "token" if token else "session"
            if token:
                self._token = token
            self._authed = True
            logger.info("MCS 面板登录成功（%s 模式）", self._auth_mode)
            return
        if self.api_key:
            raise MCSAuthError(
                "MCS 面板拒绝了 API Key（疑似 MCSM v9 面板，不支持 apikey 参数访问面板接口）。"
                "请在插件配置中填写 mcs_username + mcs_password 改用账号密码登录。"
            )
        raise MCSAuthError(
            "未配置 MCS 面板凭据：请在插件配置中填写 mcs_api_key，"
            "或 mcs_username + mcs_password"
        )

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            # MCSManager 的 CSRF 防护：所有 API 请求必须携带该头，否则返回
            # [Forbidden] 无法找到请求头 x-requested-with: xmlhttprequest
            "X-Requested-With": "XMLHttpRequest",
        }

    def _auth_params(self) -> dict:
        """当前认证模式对应的 URL 查询参数（v10 一律走 URL 参数）。"""
        if self._auth_mode == "apikey":
            return {"apikey": self.api_key}
        if self._auth_mode == "token":
            return {"token": self._token}
        return {}

    # ------------------------------------------------------------------ HTTP
    async def _request(self, method: str, path: str, params: dict = None, **kwargs):
        """认证后的请求：自动注入 apikey/token 查询参数。"""
        await self.ensure_auth()
        merged = dict(params or {})
        merged.update(self._auth_params())
        return await self._raw_request(method, path, params=merged, **kwargs)

    async def _raw_request(self, method: str, path: str, params: dict = None, **kwargs):
        url = self.base_url + path
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        # 复用同一会话以保持登录态（v9 的 session cookie）
        async with self._session.request(
            method, url, headers=self._headers(), params=params, **kwargs
        ) as resp:
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = None
            if payload is None:
                raise MCSApiError(
                    f"HTTP {resp.status}：面板返回非 JSON（请检查地址/端口，"
                    "或面板未开启 API）"
                )
            # v10: status 为数字（200 成功 / 4xx 错误）；v9: status 为 "ok"/"error"
            status = payload.get("status")
            bad = resp.status >= 400
            if isinstance(status, str):
                bad = bad or status.lower() == "error"
            else:
                try:
                    bad = bad or int(status) >= 400
                except (TypeError, ValueError):
                    pass
            if bad:
                msg = (
                    payload.get("message")
                    or payload.get("reason")
                    or payload.get("data")
                    or "未知错误"
                )
                if isinstance(msg, (dict, list)):
                    msg = str(msg)
                raise MCSApiError(f"MCS 面板错误：{msg}")
            return payload.get("data")

    # ------------------------------------------------------------------ 查询
    async def get_remote_services(self) -> list:
        """获取所有远程服务（守护进程）列表，含各守护进程下的 instances。"""
        return await self._request("GET", "/api/service/remote_services")

    async def list_instances(self) -> list:
        """聚合所有守护进程下的实例列表。

        Returns:
            [{daemon_id, instanceUuid, config, info, started, status}]
        """
        services = await self.get_remote_services()
        result = []
        for svc in services or []:
            daemon_id = svc.get("uuid") or svc.get("id")
            for inst in svc.get("instances") or []:
                if not isinstance(inst, dict) or not inst.get("instanceUuid"):
                    continue
                result.append(
                    {
                        "daemon_id": daemon_id,
                        "instanceUuid": inst.get("instanceUuid"),
                        "config": inst.get("config") or {},
                        "info": inst.get("info") or {},
                        "started": inst.get("started", 0),
                        "status": inst.get("status", 0),
                    }
                )
        return result

    async def get_instance(self, daemon_id: str, instance_uuid: str) -> dict:
        """查询单个实例详细信息（config + info + processInfo 等）。"""
        return await self._request(
            "GET", "/api/instance",
            params={"daemonId": daemon_id, "uuid": instance_uuid},
        )

    # ------------------------------------------------------------------ 操作
    async def _protected_op(
        self, action: str, daemon_id: str, instance_uuid: str, extra: dict = None
    ):
        """v10 实例操作接口：GET /api/protected_instance/<action>。"""
        params = {"uuid": instance_uuid, "daemonId": daemon_id}
        if extra:
            params.update(extra)
        return await self._request("GET", f"/api/protected_instance/{action}", params=params)

    async def open_instance(self, daemon_id: str, instance_uuid: str):
        """启动实例（异步任务，实际启动在守护进程侧进行）。"""
        return await self._protected_op("open", daemon_id, instance_uuid)

    async def close_instance(self, daemon_id: str, instance_uuid: str):
        """正常停止实例。"""
        return await self._protected_op("stop", daemon_id, instance_uuid)

    async def restart_instance(self, daemon_id: str, instance_uuid: str):
        """重启实例。"""
        return await self._protected_op("restart", daemon_id, instance_uuid)

    async def kill_instance(self, daemon_id: str, instance_uuid: str):
        """强制停止实例。"""
        return await self._protected_op("kill", daemon_id, instance_uuid)

    async def send_command(self, daemon_id: str, instance_uuid: str, command: str):
        """向实例发送控制台指令（如 say、op、whitelist、give 等）。"""
        return await self._protected_op(
            "command", daemon_id, instance_uuid, extra={"command": command}
        )

    # ------------------------------------------------------------------ 资源
    async def close(self) -> None:
        """关闭底层 HTTP 会话（后台任务结束时调用，避免连接泄漏）。"""
        if self._session is not None:
            await self._session.close()
            self._session = None
