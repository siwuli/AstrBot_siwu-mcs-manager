# -*- coding: utf-8 -*-
"""MCSManager 面板 API 客户端（v10）。

支持两种认证方式：
1. API Key：请求头 `X-Submit-Key`（推荐，长期有效）
2. 账号密码：`POST /api/auth/login` 换取 Bearer token

MCSM 响应统一为 `{"status": "ok"|"error", "data": ..., "message": ...}`，
`status == "error"` 时抛出 `MCSApiError`；认证类错误抛 `MCSAuthError`。
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

    # ------------------------------------------------------------------ 认证
    async def ensure_auth(self) -> None:
        """确保已具备认证凭据：优先 API Key；否则用账号密码登录获取 token。"""
        if self.api_key:
            return
        if self._token:
            return
        if not self.username or not self.password:
            raise MCSAuthError(
                "未配置 MCS 面板凭据：请在插件配置中填写 mcs_api_key，"
                "或 mcs_username + mcs_password"
            )
        data = await self._raw_request(
            "POST", "/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        token = (data or {}).get("token")
        if not token:
            raise MCSAuthError("MCS 面板登录失败：返回结果中没有 token，请检查账号密码")
        self._token = token
        logger.info("MCS 面板登录成功")

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            # MCSManager 的 CSRF 防护：所有 API 请求必须携带该头，否则返回
            # [Forbidden] 无法找到请求头 x-requested-with: xmlhttprequest
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.api_key:
            headers["X-Submit-Key"] = self.api_key
        elif self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ------------------------------------------------------------------ HTTP
    async def _request(self, method: str, path: str, **kwargs):
        await self.ensure_auth()
        return await self._raw_request(method, path, **kwargs)

    async def _raw_request(self, method: str, path: str, **kwargs):
        url = self.base_url + path
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            async with session.request(
                method, url, headers=self._headers(), **kwargs
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
                if str(payload.get("status")) == "error" or resp.status >= 400:
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
        """获取所有远程服务（守护进程）列表。"""
        return await self._request("GET", "/api/service/remote_services")

    async def list_instances(self) -> list:
        """聚合所有守护进程下的实例列表。

        Returns:
            [{daemon_id, instanceUuid, config, info}]
        """
        services = await self.get_remote_services()
        result = []
        for svc in services or []:
            daemon_id = svc.get("uuid") or svc.get("id")
            for inst in svc.get("instances") or []:
                result.append(
                    {
                        "daemon_id": daemon_id,
                        "instanceUuid": inst.get("instanceUuid"),
                        "config": inst.get("config") or {},
                        "info": inst.get("info") or {},
                    }
                )
        return result

    async def get_instance(self, daemon_id: str, instance_uuid: str) -> dict:
        """查询单个实例详细信息（config + info + 玩家列表等）。"""
        return await self._request(
            "GET", "/api/service/remote_service_instances",
            params={"daemonId": daemon_id, "instanceUuid": instance_uuid},
        )

    # ------------------------------------------------------------------ 操作
    async def open_instance(self, instance_uuid: str):
        """启动实例（异步任务，实际启动在守护进程侧进行）。"""
        return await self._request(
            "POST", "/api/instance/open", json={"uuid": instance_uuid}
        )

    async def close_instance(self, instance_uuid: str):
        """正常停止实例。"""
        return await self._request(
            "POST", "/api/instance/close", json={"uuid": instance_uuid}
        )

    async def restart_instance(self, instance_uuid: str):
        """重启实例。"""
        return await self._request(
            "POST", "/api/instance/restart", json={"uuid": instance_uuid}
        )

    async def kill_instance(self, instance_uuid: str):
        """强制停止实例。"""
        return await self._request(
            "POST", "/api/instance/kill", json={"uuid": instance_uuid}
        )

    async def send_command(self, instance_uuid: str, command: str):
        """向实例发送控制台指令（如 say、op、whitelist、give 等）。"""
        return await self._request(
            "POST", "/api/instance/command",
            json={"uuid": instance_uuid, "command": command},
        )
