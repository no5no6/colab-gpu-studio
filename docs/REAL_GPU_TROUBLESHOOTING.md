# Colab GPU Studio 真机冒烟排障与运维经验

本文沉淀本机部署 Colab GPU Studio 并完成真机 GPU 冒烟验收过程中的关键配置、缺陷定位与回归校验方法。全文已脱敏，不含密码、OAuth 密钥、访问令牌或个人账号信息。

## 1. 本地 OAuth 回调与运行环境

本地通过 `http://127.0.0.1:8788` 访问并完成 Google OAuth 授权时，需要在 `.env` 中配置 `ENVIRONMENT=development`、`SITE_URL=http://127.0.0.1:8788` 与 `BIND_IP=127.0.0.1`。

生产模式下应用会强制校验 HTTPS 回调地址并启用 Secure Cookie，本地 HTTP 环回地址会触发回调拦截或授权后会话丢失。开发模式允许本地 `127.0.0.1` 的 HTTP 回调与 Cookie 正常写入。

在 Google Cloud Console 创建 Web 应用凭据时，以下两项必须与 `SITE_URL` 完全一致。

- 已获授权的 JavaScript 来源填写 `http://127.0.0.1:8788`
- 已获授权的重定向 URI 填写 `http://127.0.0.1:8788/api/gpu/auth/google/callback`

## 2. Google API 权限与 SERVICE_DISABLED 排查

授权成功后若探测显卡或提交任务返回 `SERVICE_DISABLED`，通常是对应的 Google Cloud 项目尚未启用所需 API。

- 在 Google Cloud Console 的 API 与服务库中同时启用 **Colaboratory API** 与 **Google Drive API**
- 当 OAuth 同意屏幕处于测试状态时，将使用的 Google 账号加入测试用户列表
- 导入客户端 JSON 后，分别完成 Colab 与 Drive 两项授权，并在账号列表开启“允许分配”

## 3. 双层预算校验机制

系统在提交真机任务前会连续检查两层预算，任一层超出上限都会阻止任务创建。

| 预算层级 | 配置位置 | 主要参数 | 作用范围 |
| --- | --- | --- | --- |
| 全局预算 | 项目根目录 `.env` | `GPU_UNIT_RATE`、`GPU_DAILY_UNITS`、`GPU_MONTHLY_UNITS`、`GPU_MAX_JOB_UNITS` | 约束整个工作台的估算费率、单任务及日/月总额度 |
| 账号安全预算 | 网页设置页账号目录 | 单账号每日安全预算（CU） | 独立约束单个 Google 账号的日消耗上限 |

排查预算报错时应同时核对 `.env` 全局上限与设置页单账号安全预算。修改 `.env` 后需要重启工作台进程才能生效。

## 4. 真机冒烟向导已修复缺陷

本次真机联调定位并修复了三类导致误选卡、状态回退或重试卡死的缺陷。

### 4.1 冒烟机型被向导通用选卡覆盖

工作流 manifest 已通过 `compute_spec` 声明目标机型（例如 `T4`），但向导初始化会拉取账号可用规格列表并传入通用默认规格，容易在可用列表含 `A100` 等高配卡时覆盖模板指定机型。

修复位于 `api/app/review_routes.py`。提交真实冒烟时优先使用 `row["manifest"].get("compute_spec")`，仅在 manifest 未声明时回退到前端传入的 `requested_key`。

### 4.2 运行中中间态被旧失败记录遮挡或误判为失败

调度器任务生命周期包含 10 个活跃状态，即 `queued`、`validating`、`waiting_for_capacity`、`allocating`、`preparing`、`loading`、`running`、`uploading`、`cleaning_up`、`cleanup_pending`。此前前后端仅识别了其中少量状态，导致任务处于 `allocating`、`preparing`、`loading`、`uploading` 等阶段时出现以下问题。

- 后端激活快照排序把旧失败记录排在当前运行中任务之前
- 可复用任务查询漏掉中间态，重试时会重复申请 GPU
- 验收步骤状态把中间态误判为 `failed`
- 前端向导无法自动恢复轮询

修复措施如下。

- 后端在 `api/app/review_routes.py` 引入 `ACTIVE_STATES`，统一用于 `_reusable_smoke_job()`、激活任务优先级排序以及验收步骤状态计算
- 前端在 `web/src/lib/activation-resume.ts` 将上述全部活跃中间态加入 `ACTIVE_SMOKE_STATES`，并重新构建输出到 `web/dist`

### 4.3 失败重试复用旧幂等键导致一直命中旧失败任务

后端对相同 `Idempotency-Key` 会直接返回已落库的旧任务。前端原有 `smokeIdempotencyKey()` 在 `sessionStorage` 中永久缓存按工作流、项目与素材组合生成的键，当上一轮冒烟失败后再次点击重试时，请求仍携带旧键并再次命中旧失败任务。

修复措施如下。

- 在 `web/src/lib/activation-resume.ts` 新增 `isSmokeFailed()` 与 `clearSmokeIdempotencyKey()`
- 在 `web/src/components/WorkflowActivation.tsx` 的 `runAll()` 中，确认当前快照为失败或取消终态后，正式提交新冒烟前清除旧幂等键并生成新键
- 当网络超时、响应丢失或服务端尚未产生失败任务快照时，保留原有幂等键以防重复开卡扣费

## 5. 本地存储与真机验收清单

本地运行推荐在 `.env` 中保持 `GPU_STORAGE=local`，生成产物会落盘到项目目录 `data/assets/artifacts/`。

每次真机冒烟完成后，建议按以下清单完成收尾核对。

1. 核对产物文件已落盘且图片尺寸与摘要校验有效
2. 核对 Colab GPU 实例已释放，避免空闲实例持续消耗算力单元
3. 核对工作流已完成冒烟验收并发布为当前版本
4. 回归测试使用数据库临时只读备份副本运行，避免在排查过程中误提交新的真实 GPU 任务

## 6. 回归验证与部署步骤

更新前后端代码后，按以下步骤完成无扣费验证并重新启动服务。

1. 前端目录执行 `node --test web/tests/*.test.mjs`、`npm --prefix web run typecheck`、`npm --prefix web run test:contract`、`npm --prefix web run build`
2. 后端目录执行 `uv run pytest -q` 与 `uv run ruff check api tests`
3. 确认 `web/dist` 为最新构建产物后启动服务，核对 `http://127.0.0.1:8788/api/health` 返回 200
