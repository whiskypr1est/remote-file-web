# 多用户改造方案（A 档）

> 状态：**已与用户逐条确认**，2026-09-14。按本文档分阶段实施，每阶段跑一次全套测试。
> 基线提交：`70ace22`（多用户改造从这里开始）。

## 〇、适用范围与明确不做的事

目标是实验室一台高性能 Windows 主机：导师用管理员账号看全局，若干学生子账号各用自己那块
文件夹，并且**用命令行跑 PyTorch**。

**明确接受的前提（用户已确认）**：

> **子用户拥有全权限 cmd，所以「只能看到自己的文件夹」是界面便利，不是安全边界。**

具体地，子用户在 cmd 里可以读别人的目录、读 `config.json` 拿到 `session_secret`
（可伪造管理员会话）、改 `users.json` 给自己升权、`taskkill` 掉服务、杀掉同学的训练进程。
用户明确表示「都是组内学生，没关系」，并选择**维持现状的服务账号**（不降权）。

因此本文档里的「权限」一律理解为**界面与接口层面的约定**，用于防止误操作、保持日常体验清爽，
**不作为对抗恶意用户的机制**。用户管理与登录审计同理。

**A 档不做**：每用户磁盘配额、细粒度权限（只读/可写之外的）、每用户 OS 身份隔离、
真正的 OS 级隔离（那需要每用户一个服务实例或依赖 OS ACL，属于部署形态变更）。

## 一、已确认的决定

| # | 决定 |
|---|---|
| 1 | 服务账号：**维持现状**，接受风险（不改为低权限账号） |
| 2 | 命令行：`idle_timeout_seconds = 0`（不自动回收）；`max_sessions` 改为**每用户 5** |
| 3 | 每个子用户**可以有多个根**；额外提供**全员只读的公共根** |
| 4 | 子用户私有根 **不是只读** |
| 5 | 子用户初始密码由**管理员设定**（不做强制首次改密） |
| 6 | 支持**停用**用户（保留其文件夹数据），不是只能删除 |
| 7 | 「登录情况」两个都做：**在线列表** + **落盘审计日志** |
| 8 | 管理员可看每个用户的**活跃会话数 + 进程**，**不看终端输出内容** |
| 9 | 任务管理器**对子用户开放**（共享主机上"谁在占资源"是刚需） |
| 10 | 现有 `desktop_shortcuts.json` / `user_state.json` **归管理员**（他的桌面不变），子用户各存一份 |
| 11 | 缺少 `users.json` 时，把 `config.json` 的 `auth` 当作管理员（现有口令继续有效） |
| 12 | 渐进式：先做用户存储与每用户根目录，再加管理界面与审计 |

## 二、数据模型：`users.json`

放在项目根目录（与 `desktop_shortcuts.json` / `user_state.json` 同级，便于一起备份）。
沿用 `shortcuts.py` / `userstate.py` 的写法：独立文件 + 原子写 + 损坏时容错。

```jsonc
{
  "version": 1,
  "users": [
    {
      "username": "admin",              // 登录名，唯一；仅字母数字下划线连字符
      "display_name": "导师",            // 界面显示名（可中文）
      "role": "admin",                  // admin | user
      "password_hash": "pbkdf2_sha256$...",
      "enabled": true,                  // 停用后不能登录，且已有会话立即失效
      "token_version": 1,               // ★ 改密码/停用时 +1，等于"强制该用户下线"
      "created": 1789000000,
      "created_by": "",
      "last_login": 0,
      "note": "",
      // roots 为空 = 走 mount_all_drives（管理员的"全机"）；子用户必须显式给
      "roots": [
        { "id": "private", "name": "我的空间", "path": "D:\\Lab\\student1", "readonly": false },
        { "id": "public",  "name": "公共数据集", "path": "D:\\Lab\\Public",  "readonly": true }
      ],
      "max_terminal_sessions": 5,
      "permissions": {
        "terminal": true,
        "sysmon": true
      }
    }
  ]
}
```

要点：

- **`roots` 内联在用户上**，不做"权限组"抽象 —— 需求就是"每人不同"，内联最直观，
  以后真要分组再说。
- **`token_version`** 是每用户会话失效的关键。现在 `session_secret` 是全局单值，
  一个用户改密码会踢掉**所有人**（上一轮刚做的行为）；多用户下必须改成 per-user。
- **`enabled: false`** 既拒绝登录，也让已签发的会话立即失效（同样靠 `token_version`）。

## 三、认证与会话改造

现在的会话是**无状态签名 Cookie**，payload `{u, iat, exp}`，用全局 `session_secret` 签名。
改造后：

- token payload 增加 **`v`**（签发时的 `token_version`）。校验时比对用户当前的 `token_version`，
  不一致即视为已失效 —— 这就实现了"按用户踢下线"。
- **角色不进 token**，每次请求从 `users.json` 实时查。这样降权立刻生效，
  不必等会话过期。
- `login`：查用户 → 检查 `enabled` → `verify_password` → 检查通过后签发 `{u, v, iat, exp}`。
- `POST /api/auth/password`（上一轮刚做的）改为**写 `users.json` 里当前用户**，并把该用户的
  `token_version` +1（等于"我改完密码，我的其它设备全部下线"，而不是踢掉所有人）。
- 新增依赖：`require_admin`（用于用户管理、在线列表、审计查看）。

## 四、每用户的根目录（核心结构改动）

现在 `PathResolver` 是**进程级、启动时构建一次**，存在 `AppState` 上被所有请求共享
（`deps.py` 的 `_build_resolver`，共 **32 处** `state.resolver` 引用，跨 5 个文件）。

改法：**按用户构建并缓存**。

```python
# deps.AppState
def resolver_for(self, user) -> PathResolver:
    """按用户名缓存；用户根目录变更时由 users 模块调 invalidate() 清掉。"""
```

各路由从 `state.resolver` 改为取当前用户的 resolver。**不用 contextvar 隐式传递** ——
后台任务跑在普通 `threading.Thread` 里不继承 contextvar，会把任务队列砸掉。

`protected_paths` 只对管理员有意义（子用户根本看不到那些路径），保持全局配置。

**子用户的「此电脑」= 他的 roots**；没有 `mount_all_drives`，看不到磁盘列表。

## 五、横切面（容易漏的地方）

| 面 | 处理 |
|---|---|
| **后台任务归属** | `jobs.py` 目前**没有 owner**，任何登录用户能列出并取消别人的任务。加 `owner` 字段；列表按 owner 过滤；取消要校验归属；管理员可看全部 |
| **终端每用户上限** | `max_sessions` 由全局 4 改为**每用户 5**（用户决定）+ 保留全局上限防总量失控；`idle_timeout_seconds = 0` |
| **终端会话归属** | 给会话记录 owner，管理员接口只回「某用户活跃会话数」，**不回输出内容** |
| **快捷方式 / 界面状态** | 现在各是一个单文件，多人会互相覆盖。按用户分文件：`desktop_shortcuts.<username>.json` / `user_state.<username>.json`；现有那份归 `admin`（用户桌面不变） |
| **搜索** | 目标根取当前用户的 roots（改完 resolver 自动生效） |
| **缩略图 / Office 缓存** | 按文件路径共享，跨用户命中无害，不改 |
| **`/api/system/info`** | 要按当前用户返回他的 roots 与 features（terminal/sysmon 是否有权使用） |
| **壁纸** | 全局设置（子用户不单独设）。若要每人一份，属于后续增强 |

## 六、在线列表 + 审计日志

- **在线列表**（内存，重启即丢）：`{username: {ip, login_at, last_seen, session_count}}`。
  每个已认证请求刷新 `last_seen`（节流，例如 30 秒一次）。「在线」= 最近 5 分钟有活动。
  > 为什么是内存：会话是无状态签名 Cookie，服务端**本来就无法枚举**有效会话，
  > 只能记录"我见过谁"。重启后在线表清空是可接受的 —— 持久记录由审计日志承担。
- **审计日志**（落盘 JSONL 追加）：登录成功 / 登录失败 / 登出 / 改密 / 用户增删改停用，
  含时间戳、用户名、IP、结果。文件 `audit.log.jsonl`，按大小轮转，读取接口只回最近 N 条。
  管理员窗口可查。

## 七、实施阶段（每阶段跑一次全套测试并单独提交）

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **0** | git 基线 | ✅ `70ace22` |
| **1** | `users.json` + `fileweb/users.py`（增删改查、校验、原子写、容错）+ **迁移**（无 users.json 时从 `config.auth` 派生管理员） | 新模块 + `tests/test_users.py` |
| **2** | 认证改造：登录查用户表、`token_version` 校验、`require_admin`、改密码写 users.json | `auth.py` / `deps.py` / `security.py` |
| **3** | 每用户 resolver（`resolver_for`）+ **32 处调用点**改造 | `deps.py` + 5 个路由文件 |
| **4** | 横切面：终端每用户上限与 idle=0、任务归属、按用户分状态文件 | `terminal.py` / `jobs.py` / `shortcuts.py` / `userstate.py` |
| **5** | 管理界面：用户管理 + 在线列表 + 审计查看 | `routers/users.py` + `static/js/usermgr.js` |
| **6** | 子用户视角收口：`/api/system/info` 按用户返回、隐藏磁盘列表、入口按权限显隐 | `system.py` / `desktop.js` |
| **7** | 文档（README / config.example.json）+ 全套回归 | README |

**验收硬指标**：现有 **267 条测试必须保持全绿**（阶段 1 的迁移设计就是为了这个 ——
无 `users.json` 时行为与现在完全一致）。新增测试重点：
「子用户拿不到别人的根」「停用后立即失效」「token_version 生效」「任务归属隔离」。

## 八、已知的、接受了的风险（写下来免得以后误判）

1. **全权限终端 ⇒ 无真实隔离**（见第〇节）。子用户可以看同学的文件、伪造管理员会话。
2. **服务以 SYSTEM（或等价高权限）运行**：终端里的误操作（`del /s`）可以直接毁掉系统。
   应用层的 `protected_paths` **拦不住终端**。
3. 缩略图/Office 缓存按路径共享：权限被撤销后，缓存里可能仍留着已无权访问文件的缩略图。
4. 登录失败锁定**按 IP**：同一 NAT 后的多个用户会互相影响（现有行为，未改）。
