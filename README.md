# 兔兔 - AstrBot MCS 服务器管理插件

通过 **MCSManager 面板 API** 管理部署在 MCS 平台上的 Minecraft 服务器实例：实例列表、状态查询、启动/停止/重启/强制停止、发送控制台指令（say、op、白名单、give、tp 等），支持 **Agent 对话自动判断触发**。

> 插件 id：`mcs_manager`　当前版本：`1.1.3`

## 触发方式

### 1. Agent 自动判断触发（走 LLM）

本插件注册了 `mcs_*` 系列 LLM 函数工具，**Agent 会根据对话语义自动调用**。例如：

- 「帮我启动生存服」
- 「XX服还在运行吗，在线多少人」
- 「在生存服执行 op Steve」
- 「给SMP服发个公告：say 大家好」
- 「有哪些服务器？」

### 2. 命令回退（需 @ 机器人或唤醒词）

```
mc列表
mc查询 <服务器名称>
mc启动 <服务器名称>
mc停止 <服务器名称>
mc重启 <服务器名称>
mc强停 <服务器名称>
mc命令 <服务器名称> <控制台指令>
```

- 例：`兔兔mc启动 生存服`
- 例：`@兔兔 mc命令 生存服 say 大家好`

> 服务器名称支持 MCSManager 里配置的实例昵称，也支持实例 UUID。名称不唯一时会有多候选提示。

## 功能说明

1. **实例列表**：列出面板上所有服务器（名称/状态/在线玩家/端口）
2. **状态查询**：运行状态、在线玩家数、端口、启动命令
3. **启动/停止/重启/强停**：工具返回操作结果数据，由 **LLM 组织回复**；服务器达到目标状态时**自动推送通知**（后台轮询确认，超时静默）
4. **控制台指令**：say、op、whitelist、give、tp、kick、ban、list、gamemode 等任意服务端指令
5. **指令白名单**：可在配置中限制允许发送的指令前缀（默认允许全部），防止群聊里乱发危险指令
6. **操作权限校验**：启动/停止/重启/强停/发指令等操作仅允许管理员执行（`mcs_admin_ids` 白名单或群主/管理员角色）；实例列表/状态查询不受限。另内置危险指令黑名单（默认拦截 `stop`/`restart`），防止通过控制台指令绕过面板操作
7. **认证方式**：自动兼容 v10 / v9 —— 配置了 API Key 会先以 `X-Submit-Key` 探测（v10 有效）；失败自动回退账号密码登录（v10 用 Bearer token、v9 用 session cookie）。**MCSM v9 面板请填 `mcs_username` + `mcs_password`**

## Agent 工具一览

| 工具 | 说明 |
| --- | --- |
| `mcs_list_instances` | 列出所有服务器实例 |
| `mcs_instance_status` | 查询指定实例状态详情 |
| `mcs_start_instance` | 启动指定实例 |
| `mcs_stop_instance` | 正常停止指定实例 |
| `mcs_restart_instance` | 重启指定实例 |
| `mcs_kill_instance` | 强制停止指定实例 |
| `mcs_exec_command` | 发送控制台指令 |

## 管理面板配置

安装后在 AstrBot 管理面板的插件配置中修改：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `mcs_enabled` | 插件总开关 | `true` |
| `mcs_base_url` | MCSManager 面板地址（MCSM 默认端口 23333） | `http://127.0.0.1:23333` |
| `mcs_api_key` | 面板 API Key（请求头 `X-Submit-Key`，推荐） | 空 |
| `mcs_username` | 面板用户名（`mcs_api_key` 留空时登录用） | 空 |
| `mcs_password` | 面板密码 | 空 |
| `mcs_api_timeout` | 面板接口请求超时（秒） | `15` |
| `mcs_operation_wait` | 操作后状态确认等待秒数（超时静默） | `90` |
| `mcs_command_whitelist` | 控制台指令白名单（每行一个前缀；空=允许全部） | `[]` |
| `mcs_permission_enabled` | 操作权限校验总开关 | `true` |
| `mcs_admin_ids` | 可操作服务器实例的用户 ID 白名单（如 QQ 号，每行一个） | `[]` |
| `mcs_admin_role` | 群内可操作服务器实例的角色（如 `owner`、`admin`） | `["owner", "admin"]` |
| `mcs_blocked_commands` | 控制台危险指令黑名单（按首词匹配，如 `stop`、`restart`） | `["stop", "restart"]` |
| `mcs_force_agent_tool` | 检测到管理意图时强制 Agent 调用 `mcs_*` 工具 | `true` |

> `mcs_command_whitelist` 示例：`say`、`op`、`whitelist`、`list`。填写后仅允许以这些前缀开头的指令。
>
> 权限说明：开启 `mcs_permission_enabled` 后，启动/停止/重启/强停/发指令（含 Agent 工具与 mc 系列命令）仅允许
> `mcs_admin_ids` 白名单用户，或群消息中角色属于 `mcs_admin_role` 的用户执行；私聊无群角色，仅按白名单放行。
> 只读查询（`mc列表`/`mc查询`/`mcs_list_instances`/`mcs_instance_status`）对所有用户开放。

## 依赖

- Python 包：`aiohttp`（AstrBot 环境自带，见 `requirements.txt`，安装插件时自动安装）

## 安装方法

1. 执行打包脚本，生成 `plugins/astrbot/dist/siwu-mcs-manager-<版本号>.zip`
2. 打开 AstrBot 管理面板 → 插件管理 → 安装插件 → 上传该 zip
3. AstrBot 自动解压到 `data/plugins/`、安装依赖并加载插件
4. 在插件配置中填写 `mcs_base_url` 与认证凭据（API Key 或账号密码）

### 重新打包

```bash
python plugins/astrbot/siwu-mcs-manager-1_0/build.py
```

## 版本记录

每次发版在表格最上方追加一行。

| 版本 | 更新内容 |
|---|---|
| `1.1.3` | 兼容 MCSM v9 面板：v9 的面板接口不接受 `X-Submit-Key`（会返回 `[Forbidden] 权限不足`），改为自动探测——配置了 API Key 先试 v10 方式，失败则回退账号密码登录（v9 用 session cookie、v10 用 Bearer token）；v9 面板请填 `mcs_username` + `mcs_password` |
| `1.1.2` | 新增操作权限校验：启动/停止/重启/强停/发指令仅允许 `mcs_admin_ids` 白名单或群内 `mcs_admin_role` 角色（默认群主/管理员），查询类不受限；新增 `mcs_blocked_commands` 危险指令黑名单（默认拦截 stop/restart），防止经控制台指令绕过面板操作；Agent 工具与 mc 系列命令均生效 |
| `1.1.1` | 修复所有 API 请求被面板拒绝的问题（`[Forbidden] 无法找到请求头 x-requested-with`）：所有请求头统一补充 MCSManager 必需的 `X-Requested-With: XMLHttpRequest`（CSRF 防护） |
| `1.1.0` | 工具输出机制调整：所有 Agent 工具改为返回数据文本给 LLM、由 LLM 组织回复（不再直发消息），更适配 SubAgent 委派场景；后台完成推送保留 |
| `1.0.0` | 初版：注册 7 个 Agent 工具（列表/状态/启动/停止/重启/强停/指令）；mc 系列命令回退；操作后后台轮询状态自动推送；指令白名单；支持 API Key 与账号密码两种认证 |

## 项目地址

<https://github.com/siwuli/AstrBot_siwu-mcs-manager>
