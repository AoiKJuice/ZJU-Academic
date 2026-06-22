# ZDBK Calendar Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将本科课表和考试迁移到 ZDBK，可靠处理学期安排尚未发布的情况，并让每个数据源独立刷新、保存最近成功数据和通知用户异常及恢复。

**Architecture:** 新增可独立测试的 `academic_core` 包。`ZdbkClient` 负责 ZJUAM、ZDBK 会话与数据转换；`CalendarResolver` 负责多学年校历、人工日期与学期状态；`RefreshCoordinator` 负责各来源的缓存、重试和失败隔离；`HealthNotifier` 负责通知时机。`main.py` 保留 AstrBot 工具、绑定和提醒发送，只负责编排这些模块。

**Tech Stack:** Python 3.10+、`requests`、标准库 `unittest`、AstrBot 插件 API、JSON 文件缓存。

---

## 文件范围

新增：

- `academic_core/__init__.py`：导出公共类型。
- `academic_core/models.py`：状态、错误和学期数据类型，旧缓存转换。
- `academic_core/zdbk_client.py`：ZJUAM 登录、ZDBK SSO、课表与考试。
- `academic_core/calendar_resolver.py`：校历校验、合并和状态判断。
- `academic_core/refresh_coordinator.py`：按来源刷新、重试和最近成功数据。
- `academic_core/health_notifier.py`：故障、每日重复和恢复通知规则。
- `scripts/zdbk_smoke.py`：读取现有 AstrBot 配置执行只读验证，只输出状态和条数。
- `tests/fixtures/zdbk_timetable.json`：去身份化的课表响应。
- `tests/fixtures/zdbk_exams.json`：去身份化的考试响应。
- `tests/test_models.py`
- `tests/test_calendar_resolver.py`
- `tests/test_zdbk_client.py`
- `tests/test_zdbk_smoke.py`
- `tests/test_refresh_coordinator.py`
- `tests/test_health_notifier.py`
- `tests/test_plugin_integration.py`

修改：

- `main.py`：使用新模块，按来源刷新，持续检查提醒，异常查询添加状态说明。
- `_conf_schema.json`：添加人工校历启用项和四个结束日期。
- `README.md`：更新接口、校历状态、配置和通知说明。
- `metadata.yaml`：更新版本和变更说明。

不修改：

- 现有 `data` 目录结构、绑定数据格式、提醒去重记录。
- 工具名称和用户已有命令。
- `requirements.txt`；本次不增加依赖。

## Task 1: 建立测试环境和状态模型

**Files:**

- Create: `academic_core/__init__.py`
- Create: `academic_core/models.py`
- Create: `tests/__init__.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: 写状态转换和旧缓存转换的失败测试**

`tests/test_models.py` 至少包含以下断言：

```python
import unittest

from academic_core.models import SourceHealth, SourceStatus, migrate_cache


class ModelsTest(unittest.TestCase):
    def test_source_health_round_trip(self):
        health = SourceHealth(
            status=SourceStatus.FAILED,
            last_attempt_at="2026-06-22T12:00:00+08:00",
            last_success_at="2026-06-20T10:53:00+08:00",
            last_error_code="upstream_http",
            last_error_message="学校接口返回 HTTP 504",
            failure_started_at="2026-06-22T12:00:00+08:00",
            next_retry_at="2026-06-22T12:05:00+08:00",
        )
        self.assertEqual(SourceHealth.from_dict(health.to_dict()), health)

    def test_migrate_legacy_cache_preserves_existing_payload(self):
        old = {
            "academic_refresh": "2026-06-20T10:53:00+08:00",
            "task_refresh": "2026-06-20T22:25:00+08:00",
            "class_events": [{"id": "class-1"}],
            "exam_events": [{"id": "exam-1"}],
            "task_events": [{"id": "task-1"}],
        }
        migrated = migrate_cache(old)
        self.assertEqual(migrated["class_events"], old["class_events"])
        self.assertEqual(migrated["exam_events"], old["exam_events"])
        self.assertEqual(migrated["task_events"], old["task_events"])
        self.assertEqual(migrated["source_health"]["schedule"]["last_success_at"], old["academic_refresh"])
        self.assertEqual(migrated["source_health"]["tasks"]["last_success_at"], old["task_refresh"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_models -v`

Expected: `ModuleNotFoundError: No module named 'academic_core'`。

- [ ] **Step 3: 实现公共状态模型和兼容转换**

`SourceStatus` 只允许 `healthy`、`failed`、`waiting_calendar`。`SourceHealth` 包含设计文档中的全部时间、错误和通知字段，另存 `consecutive_failures`。`migrate_cache()` 必须复制输入，不得删除旧字段；新字段结构固定为：

```python
{
    "schema_version": 2,
    "source_health": {
        "calendar": {},
        "schedule": {},
        "exams": {},
        "tasks": {},
        "pta_tasks": {},
    },
    "source_data": {
        "calendar": {},
        "schedule_templates": [],
        "schedule_events": [],
        "exam_events": [],
        "task_events": [],
        "pta_task_events": [],
    },
}
```

迁移时继续维护现有顶层 `class_events`、`exam_events` 和 `task_events`，防止旧查询与提醒失效。

- [ ] **Step 4: 执行测试**

Run: `python -m unittest tests.test_models -v`

Expected: 2 tests pass。

- [ ] **Step 5: 提交**

```powershell
git add academic_core/__init__.py academic_core/models.py tests/__init__.py tests/test_models.py
git commit -m "test: define source health and cache migration"
```

## Task 2: 实现校历合并和学期状态

**Files:**

- Create: `academic_core/calendar_resolver.py`
- Create: `tests/test_calendar_resolver.py`

- [ ] **Step 1: 写完整的校历行为测试**

测试表使用以下固定学期：

```python
THIRD_PARTY = [
    {"year": "2025-2026", "term": 0, "begin": "2025-09-15", "end": "2025-11-09", "first_week_no": 1},
    {"year": "2025-2026", "term": 1, "begin": "2025-11-10", "end": "2026-01-25", "first_week_no": 1},
    {"year": "2025-2026", "term": 2, "begin": "2026-03-02", "end": "2026-04-26", "first_week_no": 1},
    {"year": "2025-2026", "term": 3, "begin": "2026-04-27", "end": "2026-06-28", "first_week_no": 1},
]

MANUAL_NEXT = [
    {"year": "2026-2027", "term": 0, "begin": "2026-09-14", "end": "2026-11-08", "first_week_no": 1},
]
```

必须覆盖：

- `2026-06-22` 返回 `active`。
- `2026-07-20` 且下一学期已知时返回 `vacation`。
- `2026-07-20` 且下一学期未知时返回 `calendar_pending`。
- 人工日期只添加第三方缺失的 `(year, term)`。
- 第三方同一学期替换人工值。
- 人工学期缺少开始或结束日期时不采用，并返回可通知的校验问题。
- 学年格式、日期顺序、学期顺序或日期重叠错误时拒绝该配置。
- 秋冬到春夏以及 `2026-2027` 的跨学年顺序正确。
- 查询区间只返回与区间相交的已知学期。

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_calendar_resolver -v`

Expected: `ModuleNotFoundError: No module named 'academic_core.calendar_resolver'`。

- [ ] **Step 3: 实现 `CalendarResolver`**

公共接口固定为：

```python
class CalendarResolver:
    def merge(self, third_party, manual_enabled, manual_terms):
        """返回经过校验、按日期排序的学期、调休和校验问题。"""

    def state_on(self, today, terms):
        """返回 active、vacation 或 calendar_pending 及当前和下一学期。"""

    def terms_for_range(self, start, end, terms):
        """只返回与查询区间相交的学期。"""
```

实现要求：

- 合并键为 `(year, term)`，第三方优先。
- 不使用固定天数推算结束日期。
- `manual_enabled=False` 时忽略所有人工日期。
- 校历缓存可以参与第三方输入，但来源必须标为 cache。
- 没有可靠下一学期时返回 `calendar_pending`，不生成推算日期。
- `active` 和 `vacation` 不是异常；只有 `calendar_pending` 进入 `waiting_calendar`。

- [ ] **Step 4: 执行测试**

Run: `python -m unittest tests.test_calendar_resolver -v`

Expected: all tests pass。

- [ ] **Step 5: 提交**

```powershell
git add academic_core/calendar_resolver.py tests/test_calendar_resolver.py
git commit -m "feat: resolve calendar transitions without date estimates"
```

## Task 3: 实现 ZJUAM 与 ZDBK 会话

**Files:**

- Create: `academic_core/zdbk_client.py`
- Create: `tests/test_zdbk_client.py`

- [ ] **Step 1: 写脚本化 HTTP 会话测试**

使用测试内的 `ScriptedSession` 和 `FakeResponse`，不访问公网。必须验证：

- CAS 登录页提取 `execution`，公钥请求后提交加密密码。
- CAS 成功后存在 `iPlanetDirectoryPro`。
- 请求 `https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html`。
- 按 302 `Location` 请求一次后取得 `/jwglxt` 路径下的 `JSESSIONID` 和 `route`。
- 缺失任一 Cookie 时抛出带 `auth_session` 代码的 `ZdbkError`。
- 302、包含 `login_ssologin`、`cas/login` 或 `统一身份认证` 的业务响应识别为会话失效。
- 会话失效只重新登录一次；第二次仍失效时停止并返回明确异常。

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_zdbk_client.ZdbkSessionTest -v`

Expected: import or class lookup fails。

- [ ] **Step 3: 从 `main.py` 移出公共客户端代码并实现会话**

`academic_core/zdbk_client.py` 定义：

```python
class ZdbkError(RuntimeError):
    def __init__(self, code: str, user_message: str, technical_message: str = ""):
        super().__init__(technical_message or user_message)
        self.code = code
        self.user_message = user_message
        self.technical_message = technical_message or user_message


class ZdbkClient:
    SSO_URL = "https://zjuam.zju.edu.cn/cas/login?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html"
    TIMETABLE_URL = "https://zdbk.zju.edu.cn/jwglxt/kbcx/xskbcx_cxXsKb.html"
    EXAMS_URL = "https://zdbk.zju.edu.cn/jwglxt/xskscx/kscx_cxXsgrksIndex.html?doType=query&queryModel.showCount=5000"
```

保留现有 RSA 公钥加密和学在浙大 session 获取能力。登录错误分类为 `auth_credentials`、`auth_locked`、`auth_session`、`captcha_required`、`timeout`、`upstream_http`、`response_format`。日志可记录技术说明，用户消息不能包含用户名、Cookie 和请求体。

- [ ] **Step 4: 执行会话测试**

Run: `python -m unittest tests.test_zdbk_client.ZdbkSessionTest -v`

Expected: all session tests pass。

- [ ] **Step 5: 提交**

```powershell
git add academic_core/zdbk_client.py tests/test_zdbk_client.py
git commit -m "feat: establish ZDBK session through ZJUAM"
```

## Task 4: 转换 ZDBK 课表和考试数据

**Files:**

- Create: `tests/fixtures/zdbk_timetable.json`
- Create: `tests/fixtures/zdbk_exams.json`
- Create: `scripts/zdbk_smoke.py`
- Create: `tests/test_zdbk_smoke.py`
- Modify: `academic_core/zdbk_client.py`
- Modify: `tests/test_zdbk_client.py`

- [ ] **Step 1: 添加去身份化响应样本**

课表样本必须含 `kbList`，并覆盖 `kcb`、`djj`、`skcd`、`xqj`、`dsz`、`xxq`、`sfyjskc`。考试样本必须含 `items`，并覆盖 Celechron 当前源码使用的 `xkkh`、`kcmc`、`xf`、`qzkssj`、`qzksdd`、`qzzwxh`、`kssj`、`jsmc`、`zwxh`。样本不得使用真实姓名、学号、课程或座位。

- [ ] **Step 2: 写转换失败测试**

断言包括：

- `xnm` 使用完整学年，例如 `2025-2026`。
- ZDBK `xqm` 按 Celechron 当前源码使用 `1|秋`、`1|冬`、`2|春`、`2|夏`；响应中的 `xxq` 也用于校验短学期归属。
- POST body 只含 `xnm`、`xqm`、`captcha_value`。
- `kcb` 中的课程、教师和地点被转换，`djj=3`、`skcd=2` 生成第 3 至 4 节。
- `dsz=0` 为单周，`dsz=1` 为双周，其他值为每周。
- `sfyjskc=1` 的预置课程不生成事件。
- `captcha_error` 转为 `captcha_required`，不请求验证码页面。
- `null` 课表返回空列表；缺失 `kbList` 的非空 JSON 转为 `response_format`。
- 考试接口读取顶层 `items`；期中与期末都能生成事件。
- 考试日期无法解析时跳过该条并记录格式问题，不能覆盖整个成功列表。
- smoke 脚本从指定 AstrBot JSON 配置读取账号，标准输出只允许状态、HTTP 状态、原始条数、转换条数和错误代码。
- smoke 脚本的异常输出不能出现用户名、密码、Cookie、请求体或完整响应。

- [ ] **Step 3: 执行转换测试并确认失败**

Run: `python -m unittest tests.test_zdbk_client.ZdbkPayloadTest -v`

Expected: parser assertions fail。

- [ ] **Step 4: 实现请求与转换**

请求头采用 Celechron 当前实现已确认的值：

```python
ZDBK_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://zdbk.zju.edu.cn/jwglxt/xtgl/index_initMenu.html",
}
```

业务方法为：

```python
def get_classes(self, academic_year: str, term: int) -> list[dict]:
    """请求秋冬或春夏课表并转换为插件课程模板。"""

def get_exams(self) -> list[dict]:
    """请求当前账号全部考试并转换为带时区的事件。"""
```

`get_exams()` 不再接收未经接口使用的学年和学期参数。业务响应进入解析前检查 HTTP 状态、重定向、登录 HTML、验证码和 JSON 类型。仅在会话失效时重登一次。

`scripts/zdbk_smoke.py` 提供以下 CLI：

```text
python scripts/zdbk_smoke.py --config /AstrBot/data/config/astrbot_plugin_zju_academic_config.json --academic-year 2025-2026 --term summer
```

`--term` 只接受 `autumn`、`winter`、`spring`、`summer`。脚本退出码为 0 表示登录、课表和考试请求完成；接口或解析失败时退出码非 0，并以单行 JSON 输出 `error_code` 和用户说明。

- [ ] **Step 5: 执行 ZDBK 全部测试**

Run: `python -m unittest tests.test_zdbk_client tests.test_zdbk_smoke -v`

Expected: all tests pass。

- [ ] **Step 6: 提交**

```powershell
git add academic_core/zdbk_client.py scripts/zdbk_smoke.py tests/test_zdbk_client.py tests/test_zdbk_smoke.py tests/fixtures/zdbk_timetable.json tests/fixtures/zdbk_exams.json
git commit -m "feat: parse ZDBK timetable and exams"
```

## Task 5: 实现按来源刷新、缓存和重试

**Files:**

- Create: `academic_core/refresh_coordinator.py`
- Create: `tests/test_refresh_coordinator.py`

- [ ] **Step 1: 写协调器行为测试**

通过注入固定 `now` 和假数据源验证：

- schedule 失败后 exams、tasks、pta_tasks 仍被调用。
- 失败来源保留之前成功数据和 `last_success_at`。
- 成功来源单独更新数据和状态。
- 重试间隔依次为 5 分钟、15 分钟、1 小时，之后每小时。
- 未到 `next_retry_at` 的后台刷新跳过；`force=True` 允许即时尝试一次。
- `waiting_calendar` 的下一检查时间为 6 小时。
- `calendar_pending` 可保存 `schedule_templates`，但不更新 `schedule_events` 为推算数据。
- 旧缓存迁移后仍可参与刷新和提醒。
- 保存失败时内存中的最近成功数据不被部分替换。

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_refresh_coordinator -v`

Expected: module import fails。

- [ ] **Step 3: 实现协调器**

公共调用方式固定为：

```python
coordinator = RefreshCoordinator(cache, now_provider)
result = coordinator.refresh(
    sources={
        "calendar": fetch_calendar,
        "schedule": fetch_schedule,
        "exams": fetch_exams,
        "tasks": fetch_tasks,
        "pta_tasks": fetch_pta_tasks,
    },
    force=False,
)
```

每个 fetcher 返回 `SourceResult(data=..., metadata=...)`，失败时抛出带错误代码和用户说明的异常。`refresh()` 捕获每个来源的异常并继续循环，返回改变后的缓存和状态变更列表。缓存写入仍由 `main.py` 的临时文件替换完成。

- [ ] **Step 4: 执行测试**

Run: `python -m unittest tests.test_refresh_coordinator -v`

Expected: all tests pass。

- [ ] **Step 5: 提交**

```powershell
git add academic_core/refresh_coordinator.py tests/test_refresh_coordinator.py
git commit -m "feat: isolate refresh failures by source"
```

## Task 6: 实现故障、每日重复和恢复通知

**Files:**

- Create: `academic_core/health_notifier.py`
- Create: `tests/test_health_notifier.py`

- [ ] **Step 1: 写通知规则测试**

必须验证：

- `healthy -> failed` 立即产生一条通知。
- `healthy -> waiting_calendar` 立即产生一条通知。
- 同一错误在 24 小时内不重复。
- 持续异常满 24 小时后每天产生一条通知。
- 错误代码改变视为新的异常并立即通知。
- `failed` 或 `waiting_calendar` 恢复为 `healthy` 时产生一条恢复通知。
- 通知发送失败时不更新 `last_notification_at`，后续循环再次尝试。
- 多个绑定会话分别记录发送结果；一个会话失败不影响其他会话。
- 文本包含来源、用户说明、最后成功时间、旧缓存是否使用和可执行操作。
- 文本不包含账号、Cookie、完整堆栈和请求参数。

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_health_notifier -v`

Expected: module import fails。

- [ ] **Step 3: 实现通知决策**

公共接口固定为纯逻辑函数，发送动作由 `main.py` 调用 AstrBot：

```python
class HealthNotifier:
    def pending(self, before, after, recipients, now):
        """返回每个会话待发送的通知，不修改状态。"""

    def mark_sent(self, health, recipient, sent_at):
        """只在 AstrBot 确认发送成功后更新记录。"""
```

每个来源保存按 recipient 区分的 `notification_deliveries`，避免部分发送成功后重复通知全部会话。恢复通知成功后清理对应异常的待发记录。

- [ ] **Step 4: 执行测试**

Run: `python -m unittest tests.test_health_notifier -v`

Expected: all tests pass。

- [ ] **Step 5: 提交**

```powershell
git add academic_core/health_notifier.py tests/test_health_notifier.py
git commit -m "feat: notify bound sessions about source health"
```

## Task 7: 集成到 AstrBot 插件

**Files:**

- Modify: `main.py`
- Create: `tests/test_plugin_integration.py`

- [ ] **Step 1: 写不依赖 AstrBot 安装的集成测试**

将需要验证的编排逻辑放在 `academic_core`，测试使用假 fetcher、假 sender 和临时目录。覆盖：

- 首次启动读取旧 `cache.json` 后保留课程、考试和任务。
- 校历成功、课表失败时考试和任务仍更新。
- 后台刷新抛出未分类异常后，提醒检查仍执行。
- `calendar_pending` 保存课程模板，不生成课程事件，不发送课程提醒。
- 校历恢复后使用模板重新生成事件，并产生恢复通知。
- 正常 schedule 查询不带状态说明。
- schedule 异常查询带最后成功时间和旧数据说明。
- tasks 异常不会给 schedule 查询添加说明。

- [ ] **Step 2: 执行测试并确认失败**

Run: `python -m unittest tests.test_plugin_integration -v`

Expected: orchestration assertions fail。

- [ ] **Step 3: 修改插件初始化和缓存读取**

在 `ZjuAcademicPlugin.__init__` 中：

- 对 `_load_json_file(cache_path, _default_cache())` 的结果调用 `migrate_cache()`。
- 创建 `CalendarResolver`、`RefreshCoordinator` 和 `HealthNotifier`。
- 不删除 `_state` 中的 `targets`、`reminded` 和已有提醒设置。

- [ ] **Step 4: 替换本科客户端**

`_build_zju_client()` 返回新 `ZdbkClient`。删除 AppService URL 和旧本科解析器。学在浙大任务继续复用同一 ZJUAM session。考试只请求一次 `get_exams()`，不再按学期重复请求。

- [ ] **Step 5: 重写刷新编排**

`_refresh_cache(force)` 分别注册 calendar、schedule、exams、tasks、pta_tasks fetcher。schedule 使用 `CalendarResolver.terms_for_range()` 的结果构造 ZDBK 请求；同一 `(academic_year, term)` 只请求一次。每个来源结束后更新自己的状态，全部结束后原子保存缓存。

后台循环改为两个独立的异常边界：

```python
async def _background_loop(self):
    while True:
        try:
            if self._cfg_bool("auto_refresh_enabled", True):
                await self._refresh_cache(force=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("zju academic refresh loop failed")

        try:
            await self._dispatch_health_notifications()
            await self._dispatch_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("zju academic notification loop failed")

        await asyncio.sleep(max(15, self._cfg_int("loop_interval_seconds", 45)))
```

某个会话发送异常不能中止其他会话。提醒只读取仍处于有效时间窗口的最近成功事件。

- [ ] **Step 6: 添加查询状态说明**

schedule、exams、tasks 和 pta_tasks 分别读取对应 `SourceHealth`。只有当前查询的数据源为 `failed` 或 `waiting_calendar` 时，JSON 结果添加：

```python
{
    "source_status": {
        "status": "failed",
        "last_success_at": "2026-06-20T10:53:00+08:00",
        "message": "学校接口暂时不可用，以下内容来自最近一次成功数据。",
    }
}
```

健康状态不添加该字段。手动刷新发起会话收到本次各来源结果，自动故障通知仍发送给所有绑定提醒会话。

- [ ] **Step 7: 执行集成测试和语法检查**

Run: `python -m unittest tests.test_plugin_integration -v`

Expected: all tests pass。

Run: `python -m compileall -q academic_core main.py tests`

Expected: exit code 0, no output。

- [ ] **Step 8: 提交**

```powershell
git add main.py tests/test_plugin_integration.py
git commit -m "feat: coordinate AstrBot refresh and health messages"
```

## Task 8: 更新配置和用户文档

**Files:**

- Modify: `_conf_schema.json`
- Modify: `README.md`
- Modify: `metadata.yaml`
- Modify: `tests/test_plugin_integration.py`

- [ ] **Step 1: 写配置断言**

测试读取 `_conf_schema.json`，确认存在：

```python
EXPECTED_KEYS = {
    "manual_calendar_enabled",
    "manual_autumn_begin",
    "manual_autumn_end",
    "manual_winter_begin",
    "manual_winter_end",
    "manual_spring_begin",
    "manual_spring_end",
    "manual_summer_begin",
    "manual_summer_end",
}
```

`manual_calendar_enabled` 默认值必须为 `false`。四个结束日期默认值为空字符串；开始日期保留用户旧配置兼容，但只在启用且开始、结束都有效时生效。

- [ ] **Step 2: 执行配置测试并确认失败**

Run: `python -m unittest tests.test_plugin_integration.PluginConfigTest -v`

Expected: missing configuration key assertions fail。

- [ ] **Step 3: 更新配置和文档**

README 说明：

- 本科课表和考试使用 ZDBK，AppService 已停用。
- `active`、`vacation`、`calendar_pending` 的用户表现。
- 第三方校历优先，人工日期仅填充缺失学期。
- 人工学期必须同时设置开始和结束日期。
- 异常首次通知、每日重复、恢复通知和旧数据标记。
- 验证码只会提示，当前版本不提供填写界面。
- 重试时间为 5 分钟、15 分钟、1 小时和之后每小时；待校历状态每 6 小时检查。

`metadata.yaml` 按仓库现有格式更新小版本号，不声称尚未验证的服务器状态。

- [ ] **Step 4: 执行配置测试**

Run: `python -m unittest tests.test_plugin_integration.PluginConfigTest -v`

Expected: all tests pass。

- [ ] **Step 5: 提交**

```powershell
git add _conf_schema.json README.md metadata.yaml tests/test_plugin_integration.py
git commit -m "docs: explain calendar states and source notifications"
```

## Task 9: 本地验证、服务器发布和恢复演练

**Files:**

- Verify: all changed files
- Create during release: `outputs/zju-academic-update.patch`
- Server target: `/home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic`
- Server backups: `/home/ubuntu/services/astrbot/backups`

- [ ] **Step 1: 执行完整本地验证**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q academic_core main.py tests
git diff --check
git status --short
```

Expected:

- 全部测试通过。
- compileall exit code 0 且无输出。
- `git diff --check` 无输出。
- `git status --short` 只显示尚未提交的实施计划时，提交该计划后再次检查为空。

- [ ] **Step 2: 审查需求覆盖**

逐项核对设计文档验收条件。使用以下检索确认旧接口和临时内容不存在：

```powershell
rg -n "appservice\.zju\.edu\.cn|TODO|TBD|NotImplementedError" main.py academic_core tests README.md _conf_schema.json
```

Expected: AppService、TODO、TBD 无匹配；抽象基类若保留 `NotImplementedError`，必须由测试覆盖且生产实例不使用该基类。

- [ ] **Step 3: 生成发布文件**

```powershell
git format-patch --stdout 576595403abc683d1fffed3c4ff10bd74af0d97e..HEAD > C:\Users\pjjzx\Documents\Codex\2026-06-22\qian\outputs\zju-academic-update.patch
```

Expected: patch 包含设计、计划、实现、测试和文档提交。

- [ ] **Step 4: 发布前查阅 CLI 内置帮助**

在执行发布命令前查看当前环境语法：

```powershell
git format-patch -h
ssh -V
scp
ssh ubuntu@1.15.106.207 "git am -h; sudo docker restart --help; sudo docker cp --help; sudo docker exec --help"
```

记录实际输出。若语法与本计划不一致，停止发布并修订命令，不继续假定。

- [ ] **Step 5: 检查服务器状态和创建备份**

```powershell
ssh ubuntu@1.15.106.207 "sudo docker ps --filter name=astrbot; git -C /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic status --short; git -C /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic rev-parse HEAD"
ssh ubuntu@1.15.106.207 "mkdir -p /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk; cp -a /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/plugin; cp -a /home/ubuntu/services/astrbot/data/config/astrbot_plugin_zju_academic_config.json /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/config.json; cp -a /home/ubuntu/services/astrbot/data/plugin_data/astrbot_plugin_zju_academic /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/plugin_data"
```

Expected: 容器状态为 Up；插件仓库无改动；HEAD 为 `576595403abc683d1fffed3c4ff10bd74af0d97e`；三个备份路径存在。任何一项不符合时停止发布并报告实际状态。

- [ ] **Step 6: 执行 ZDBK 只读验证**

使用服务器现有配置中的账号，但不得把用户名、密码、Cookie 或响应全文输出到终端。课表请求使用当前已知学年和学期，考试使用无额外筛选参数的接口：

```powershell
ssh ubuntu@1.15.106.207 "rm -rf /tmp/zdbk-smoke; mkdir -p /tmp/zdbk-smoke"
scp -r academic_core scripts/zdbk_smoke.py ubuntu@1.15.106.207:/tmp/zdbk-smoke/
ssh ubuntu@1.15.106.207 "sudo docker cp /tmp/zdbk-smoke astrbot:/tmp/zdbk-smoke"
ssh ubuntu@1.15.106.207 "sudo docker exec -e PYTHONPATH=/tmp/zdbk-smoke astrbot python /tmp/zdbk-smoke/zdbk_smoke.py --config /AstrBot/data/config/astrbot_plugin_zju_academic_config.json --academic-year 2025-2026 --term summer"
```

Expected: CAS 与 ZDBK session 成功，课表 HTTP 200，转换条数大于 0；考试 HTTP 200，允许当前无考试时为 0。出现验证码或格式错误时停止发布，向用户报告错误代码。

- [ ] **Step 7: 应用更新并重新启动容器**

```powershell
scp C:\Users\pjjzx\Documents\Codex\2026-06-22\qian\outputs\zju-academic-update.patch ubuntu@1.15.106.207:/tmp/zju-academic-update.patch
ssh ubuntu@1.15.106.207 "git -C /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic am --3way /tmp/zju-academic-update.patch"
ssh ubuntu@1.15.106.207 "sudo docker restart astrbot"
```

Expected: `git am` 完成全部提交；`docker restart` 输出 `astrbot`。

- [ ] **Step 8: 验证功能与日志**

等待 AstrBot 启动后执行：

```powershell
ssh ubuntu@1.15.106.207 "sudo docker ps --filter name=astrbot; sudo docker logs --since 10m astrbot 2>&1 | grep -E 'astrbot_plugin_zju_academic|ZDBK|zju academic|Traceback|ERROR|504'"
```

随后通过现有插件工具强制刷新一次，并验证：

- schedule、exams、tasks、pta_tasks 分别报告状态。
- 课表事件条数与只读验证转换结果一致，具体日期处于可靠校历范围。
- 任务来源失败不影响课程提醒检查。
- 正常查询不显示状态说明。
- 人为使用测试 fetcher 触发的异常只在测试环境验证，不在生产环境制造学校接口错误。
- 现有绑定数、提醒记录数和配置值与备份前一致。
- 容器日志没有 AppService 请求、未处理 Traceback 和连续 504。

- [ ] **Step 9: 发布失败时恢复**

若 Step 7 或 Step 8 失败，保存失败日志后执行：

```powershell
ssh ubuntu@1.15.106.207 "rm -rf /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic; cp -a /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/plugin /home/ubuntu/services/astrbot/data/plugins/astrbot_plugin_zju_academic; cp -a /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/config.json /home/ubuntu/services/astrbot/data/config/astrbot_plugin_zju_academic_config.json; rm -rf /home/ubuntu/services/astrbot/data/plugin_data/astrbot_plugin_zju_academic; cp -a /home/ubuntu/services/astrbot/backups/2026-06-22-zdbk/plugin_data /home/ubuntu/services/astrbot/data/plugin_data/astrbot_plugin_zju_academic; sudo docker restart astrbot"
```

恢复后重新检查容器状态、插件 HEAD、配置文件和缓存时间。报告测试失败项、发布步骤和恢复结果，不声明更新成功。

- [ ] **Step 10: 最终报告**

只在本地测试、ZDBK 只读验证、服务器刷新、查询、提醒检查和日志检查全部完成后报告完成。报告功能变化、验证结果、服务器提交号和保留的数据；任何未验证项单独列出。
