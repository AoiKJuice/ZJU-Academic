ZJU-Academic 是给 AstrBot 使用的 zdbk数据助手。它适合放在私人聊天或固定群聊里，用来查询课表、考试、DDL，并在重要时间前提醒你。

它面向日常使用，可以直接用自然语言询问，机器人会按当前配置读取 zdbk数据并回复。

## What can it do?

- 查询课表
- 查询近期或全部考试安排
- 查询“学在浙大”待办和截止时间
- 查询 PTA / Pintia 的题集截止时间
- 在上课前、考试前、DDL 截止前自动提醒
- 把课表、考试、DDL 结果发成图片，方便在聊天里查看

本科课表和考试使用 ZDBK 教务接口。旧的 AppService 接口已经停用，本插件不再依赖它。

## 谁能使用

zdbk数据属于私人信息，查询功能默认只给 AstrBot 管理员使用。非管理员询问课表、考试或 DDL 时，机器人不会返回 zdbk数据。

提醒发送到已绑定的会话。会话可以在插件设置里固定填写，也可以由管理员在聊天里让机器人绑定当前会话。

## 安装

把本目录放到 AstrBot 的插件目录后重启 AstrBot 即可。

## 第一次使用

打开 AstrBot 的插件设置，找到 `ZJU-Academic`，按下面顺序填写。

1. 填写浙大统一身份认证账号
    - `username`：统一身份认证用户名
    - `password`：统一身份认证密码
2. 检查校历设置
    默认开启 `auto_calendar_enabled`。插件会先读取本仓库的 `calendar/terms.json`，再读取 `Xecades/zju-ical-py` 维护的浙大校历配置。前者维护秋冬、春夏两个长学期的开始日期；后者继续提供旧学期数据和调休安排。

    `calendar/terms.json` 每个学年只需要维护两个日期：

    - `autumn_winter.begin`：秋冬学期开始日期
    - `spring_summer.begin`：春夏学期开始日期

    插件按固定 16 周计算结束日期，秋冬映射为秋、冬两个内部学期，春夏映射为春、夏两个内部学期。
    
    人工校历默认关闭。只有在自动校历来源暂未发布某个学期时，才建议打开 `manual_calendar_enabled` 并填写人工日期。自动来源和人工日期同时存在时，以自动来源为准。

    人工学期必须同时填写开始和结束日期，插件不会用固定天数推算结束日期：
    
    - `manual_calendar_year`：当前学年，例如 `2025-2026`
    - `manual_autumn_begin`：秋学期开始日期
    - `manual_autumn_end`：秋学期结束日期
    - `manual_winter_begin`：冬学期开始日期
    - `manual_winter_end`：冬学期结束日期
    - `manual_spring_begin`：春学期开始日期
    - `manual_spring_end`：春学期结束日期
    - `manual_summer_begin`：夏学期开始日期
    - `manual_summer_end`：夏学期结束日期
    
3. 设置提醒会话
    
    先在目标聊天（主力账号和机器人的对话中）里问机器人：
    
    ```
    给我这个对话的 bound_sessions
    ```
    
    回复里会包含当前会话 ID。把它填到 `bound_sessions`，一行一个：
    
    ```
    会话ID|备注
    ```
    
    例子：
    
    ```
    aiocqhttp:GroupMessage:123456789|我
    ```
    
    也可以由管理员在目标聊天里直接说：
    
    ```
    绑定当前会话的 ZJU-Academic 提醒
    ```
    
4. 设置提醒时间
    
    默认设置适合大多数场景：
    
    - `class_reminder_offsets_minutes`：课前 30 分钟提醒
    - `exam_reminder_offsets_minutes`：考前 1 天、3 小时、30 分钟提醒
    - `task_reminder_offsets_minutes`：DDL 前 1 天、3 小时、30 分钟提醒
    
    多个时间用英文逗号分隔，例如：
    
    ```
    1440,180,30
    ```
    
    课表、校历和考试信息默认 12 小时更新一次。DDL 会按 `cache_ttl_minutes` 设置更新，默认 30 分钟。上课、考试和 DDL 提醒会按 `loop_interval_seconds` 检查，默认 45 秒。
    
5. 设置 PTA
    
    如果要看 PTA / Pintia DDL，保持 `pta_enabled` 为开启状态。
    
    PTA 登录在网页里完成。推荐在聊天里问：
    
    ```
    PTA 登录
    ```
    
    机器人会返回登录入口。如
    ```
     /zju-academic/pta-login/[token]
    ```
     在astrbot域名后加上这一段即为登录界面，例如
    
    ```
    http://xxx.com:6185/zju-academic/pta-login/[token]
    ``` 

	打开页面后输入 PTA 账号、密码，并完成验证。登录成功后，即可激活PTA提醒功能。
	
	PTA 的登录状态可能会过期。出现这种情况时，重新登录即可。
### DDL 和任务

- 不特殊说明的情况下，返回的是未来七天的任务
- 你也可以要求获取当前可以获取的所有任务

### 刷新数据

- 普通查询会使用缓存，速度更快
- 明确要求刷新时，机器人会重新获取课表、考试和任务
- 某个来源失败时，其他来源仍会继续刷新；查询会继续使用对应来源最近一次成功的数据
- 自动重试间隔为 5 分钟、15 分钟、1 小时，之后每小时一次；等待校历发布时每 6 小时检查一次

### 校历状态和异常通知

校历状态分三类：

- `active`：当前在已知学期内，课程可以生成具体日期和提醒
- `vacation`：当前不在学期内，但下个学期日期已知
- `calendar_pending`：下个学期安排尚未可靠发布，不生成推算日期和课程提醒

当课表、考试、学在浙大任务或 PTA 任务获取失败时，插件会通知所有已绑定提醒会话。首次异常会立即通知；同一异常 24 小时内不重复；持续异常满 24 小时后每天通知一次；恢复时通知一次。

查询结果只有在对应来源异常时才会包含 `source_status`。正常查询不会显示状态标记。验证码场景只会提示需要人工处理，当前版本不提供验证码填写界面。

### 查看状态

```
查看ZJU-Acaddemic插件状态
```

可以看到：

- 当前会话 ID
- 是否配置了统一认证账号
- PTA 是否可用
- 是否保存了 PTA 登录状态
- 已绑定的提醒会话数量
- 上次刷新时间
- 当前缓存里有多少课表、考试和任务

### 绑定和解绑提醒

在希望接收提醒的聊天里，由管理员发送：

```
绑定当前会话的 ZJU-Academic 提醒
```

不想在当前聊天继续收提醒时，发送：

```
解绑当前会话的 ZJU-Academic 提醒
```

查看当前聊天是否已绑定：

```
当前会话绑定了吗
```

## 配置项速查

|配置项|用途|
|---|---|
|`username` / `password`|浙大统一身份认证账号|
|`bound_sessions`|固定接收提醒的会话|
|`class_reminder_offsets_minutes`|课前提醒时间/min（用逗号分割）|
|`exam_reminder_offsets_minutes`|考前提醒时间/min（用逗号分割）|
|`task_reminder_offsets_minutes`|DDL 前提醒时间/min（用逗号分割）|
|`pta_enabled`|是否启用 PTA DDL|
|`auto_calendar_enabled`|是否自动获取校历|
|`manual_calendar_enabled`|是否启用人工校历日期|
|`manual_calendar_year`|当前学年|
|`manual_autumn_begin`|秋学期开始日期|
|`manual_autumn_end`|秋学期结束日期|
|`manual_winter_begin`|冬学期开始日期|
|`manual_winter_end`|冬学期结束日期|
|`manual_spring_begin`|春学期开始日期|
|`manual_spring_end`|春学期结束日期|
|`manual_summer_begin`|夏学期开始日期|
|`manual_summer_end`|夏学期结束日期|
|`cache_ttl_minutes`|DDL 数据刷新间隔/min|
|`render_query_as_image`|是否把查询结果发成图片|
|`query_image_font_path`|图片字体路径，默认使用插件内置 Noto 字体|
|`auto_refresh_enabled`|是否自动刷新数据|
|`loop_interval_seconds`|提醒检查间隔/s|
