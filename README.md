# astrbot_plugin_dsquota

AstrBot 插件：查询 DeepSeek 账户余额，触发指令 `/dsquota`，支持定时播报与低余额阶梯告警。

## 功能特性

- ✅ **指令查询**：发送 `/dsquota` 即可查看 DeepSeek 账户总余额、赠送余额、充值余额
- ✅ **信号灯样式**：🟢 充足 / 🟡 偏紧 / 🔴 告急，一目了然
- ✅ **定时播报**：按 Cron 表达式定时推送余额到指定群/私聊（默认关闭）
- ✅ **阶梯告警**：余额跌破设定阈值时自动推送告警，每个阈值仅触发一次（默认关闭）
- ✅ **权限控制**：可设置仅管理员可用
- ✅ **余额制**：支持 DeepSeek 的 total/granted/topped_up 三层余额结构

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/dsquota` | 查询当前 DeepSeek 账户余额 | 所有人 / 仅管理员（可配置） |

## 配置说明

在 AstrBot WebUI 插件配置页面可修改以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_key` | string | — | **必填**。DeepSeek API Key，在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 获取 |
| `admin_only` | bool | false | 是否仅管理员可用 |
| `schedule_enable` | bool | false | 开启定时播报 |
| `schedule_cron` | string | `0 9 * * *` | 定时播报 Cron 表达式 |
| `schedule_targets` | string | — | 推送目标（同 schedule_target_id + schedule_target_type） |
| `monitor_enable` | bool | false | 开启阶梯告警 |
| `monitor_alarm_thresholds` | string | `20,10,5,1` | 告警阈值（元），逗号分隔递减 |
| `monitor_targets` | string | — | 告警目标（同 monitor_target_id + monitor_target_type） |

## 依赖

- `httpx>=0.27.0`
- `apscheduler>=3.10.0`（定时/监控功能需要）

## 安装

1. 将本仓库克隆到 AstrBot 的 `addons/` 目录下
2. 在 AstrBot WebUI 插件管理页面刷新并启用
3. 在插件配置中填写 DeepSeek API Key
4. (可选) 开启定时播报或阶梯告警功能

## 类似项目

- [astrbot_plugin_minimax_quota](https://github.com/konley/astrbot_plugin_minimax_quota) — MiniMax 限额查询插件

## License

MIT
